from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import (
    ALERT_COOLDOWN_SECONDS,
    DELIVERY_CLAIM_TTL_SECONDS,
    OBJECT_STORAGE_BUCKET,
    OBJECT_STORAGE_ENDPOINT,
)
from .models import MatchNormalized

logger = logging.getLogger(__name__)


class StorageUnavailableError(Exception):
    """Raised when Object Storage is not configured or cannot be reached."""


@dataclass(frozen=True)
class DeliveryClaim:
    match_uid: str
    key: str
    claim_id: str
    etag: str | None = None


@dataclass(frozen=True)
class PendingDelivery:
    """A durable result delivery waiting for a Telegram confirmation."""

    key: str
    channel_id: str
    channel_name: str
    match: MatchNormalized
    created_at: str
    last_attempt_at: str | None = None
    attempt_count: int = 0


RESULT_OUTBOX_PREFIX = "outbox/results/"
TELEGRAM_MEDIA_HEALTH_KEY = "delivery-health/telegram-media.json"
CLAIM_CREATE_MAX_ATTEMPTS = 3


def _client():
    return boto3.client("s3", endpoint_url=OBJECT_STORAGE_ENDPOINT)


def _bucket() -> str:
    if not OBJECT_STORAGE_BUCKET:
        raise StorageUnavailableError("OBJECT_STORAGE_BUCKET is not configured")
    return OBJECT_STORAGE_BUCKET


def processed_key(match_uid: str) -> str:
    return f"processed/{match_uid}.json"


def claim_key(match_uid: str) -> str:
    return f"claims/{match_uid}.json"


def result_outbox_key(match: MatchNormalized, channel_id: str) -> str:
    return f"{RESULT_OUTBOX_PREFIX}{channel_match_uid(match, channel_id)}.json"


def alert_key(alert_code: str, now: datetime) -> str:
    window = int(now.timestamp()) // ALERT_COOLDOWN_SECONDS
    return f"alerts/{safe_storage_part(alert_code)}/{window}.json"


def safe_storage_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "unknown"


def logo_cache_key(url: str) -> str:
    """Return an opaque, versioned Object Storage key for a logo URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"media/logos/{digest}.png"


def read_cached_logo(
    url: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> bytes | None:
    """Read a cached logo, treating cache and storage failures as misses."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = logo_cache_key(url)
    try:
        response = s3.get_object(Bucket=bucket_name, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()
    except ClientError as exc:
        if _is_not_found(exc):
            return None
        logger.warning("logo_cache_read_failed key=%s error_type=%s", key, type(exc).__name__)
        return None
    except (KeyError, OSError) as exc:
        logger.warning("logo_cache_read_failed key=%s error_type=%s", key, type(exc).__name__)
        return None


def write_cached_logo(
    url: str,
    data: bytes,
    client: Any | None = None,
    bucket: str | None = None,
) -> bool:
    """Persist a validated logo without making publication depend on the cache."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = logo_cache_key(url)
    try:
        s3.put_object(Bucket=bucket_name, Key=key, Body=data, ContentType="image/png")
        return True
    except ClientError as exc:
        logger.warning("logo_cache_write_failed key=%s error_type=%s", key, type(exc).__name__)
        return False


async def write_analytics_record(
    kind: str,
    record_id: str,
    payload: dict[str, Any],
    client: Any | None = None,
    bucket: str | None = None,
) -> str:
    """Persist analytics independently of delivery claims; callers handle failures."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = f"analytics/{safe_storage_part(kind)}/{safe_storage_part(record_id)}.json"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def _put() -> None:
        s3.put_object(Bucket=bucket_name, Key=key, Body=body, ContentType="application/json")

    try:
        await asyncio.to_thread(_put)
        return key
    except ClientError as exc:
        raise StorageUnavailableError(f"analytics write failed for {key}") from exc


def channel_match_uid(match: MatchNormalized, channel_name: str) -> str:
    return f"{safe_storage_part(channel_name)}_{match.match_uid}"


def legacy_channel_match_uid(match: MatchNormalized, channel_name: str) -> str:
    return f"{safe_storage_part(channel_name)}_{match.legacy_match_uid}"


def _pending_delivery_payload(
    match: MatchNormalized,
    channel_id: str,
    channel_name: str,
    *,
    created_at: str,
    last_attempt_at: str | None,
    attempt_count: int,
) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "created_at": created_at,
            "last_attempt_at": last_attempt_at,
            "attempt_count": attempt_count,
            "match": match.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


async def enqueue_result_delivery(
    match: MatchNormalized,
    channel_id: str,
    channel_name: str,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Create a durable result outbox item without resetting existing retry state."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = result_outbox_key(match, channel_id)
    created_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    body = _pending_delivery_payload(
        match,
        channel_id,
        channel_name,
        created_at=created_at,
        last_attempt_at=None,
        attempt_count=0,
    )

    try:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket_name,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return True
    except ClientError as exc:
        if _is_precondition_failed(exc):
            return False
        raise StorageUnavailableError(f"result outbox write failed for {key}") from exc


async def list_pending_result_deliveries(
    client: Any | None = None,
    bucket: str | None = None,
    limit: int = 200,
) -> list[PendingDelivery]:
    """Load durable result deliveries, ordered by least recent attempt first."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    pending: list[PendingDelivery] = []
    continuation_token: str | None = None

    while len(pending) < max(1, limit):
        kwargs: dict[str, Any] = {
            "Bucket": bucket_name,
            "Prefix": RESULT_OUTBOX_PREFIX,
            "MaxKeys": min(1000, max(1, limit)),
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        try:
            page = await asyncio.to_thread(s3.list_objects_v2, **kwargs)
        except ClientError as exc:
            raise StorageUnavailableError("result outbox list failed") from exc

        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key.startswith(RESULT_OUTBOX_PREFIX):
                continue
            try:
                response = await asyncio.to_thread(s3.get_object, Bucket=bucket_name, Key=key)
                stream = response["Body"]
                try:
                    payload = json.loads(stream.read())
                finally:
                    stream.close()
                pending.append(
                    PendingDelivery(
                        key=key,
                        channel_id=str(payload["channel_id"]),
                        channel_name=str(payload["channel_name"]),
                        match=MatchNormalized.model_validate(payload["match"]),
                        created_at=str(payload["created_at"]),
                        last_attempt_at=(
                            str(payload["last_attempt_at"])
                            if payload.get("last_attempt_at")
                            else None
                        ),
                        attempt_count=max(0, int(payload.get("attempt_count", 0))),
                    )
                )
            except (ClientError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.error(
                    'storage_error="invalid result outbox item" key="%s" error_type="%s"',
                    key,
                    type(exc).__name__,
                )
            if len(pending) >= limit:
                break

        continuation_token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not continuation_token:
            break

    pending.sort(
        key=lambda item: (
            item.last_attempt_at is not None,
            item.last_attempt_at or item.created_at,
            item.created_at,
            item.key,
        )
    )
    return pending


async def record_result_delivery_attempt(
    pending: PendingDelivery,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> PendingDelivery:
    """Move a failed outbox item to the back of the fair retry order."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    attempted_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    updated = PendingDelivery(
        key=pending.key,
        channel_id=pending.channel_id,
        channel_name=pending.channel_name,
        match=pending.match,
        created_at=pending.created_at,
        last_attempt_at=attempted_at,
        attempt_count=pending.attempt_count + 1,
    )
    body = _pending_delivery_payload(
        updated.match,
        updated.channel_id,
        updated.channel_name,
        created_at=updated.created_at,
        last_attempt_at=updated.last_attempt_at,
        attempt_count=updated.attempt_count,
    )
    try:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket_name,
            Key=updated.key,
            Body=body,
            ContentType="application/json",
        )
        return updated
    except ClientError as exc:
        raise StorageUnavailableError(f"result outbox attempt write failed for {updated.key}") from exc


async def delete_result_delivery(
    pending: PendingDelivery,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    """Remove a durable result only after it is processed or reconciled."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    try:
        await asyncio.to_thread(s3.delete_object, Bucket=bucket_name, Key=pending.key)
    except ClientError as exc:
        raise StorageUnavailableError(f"result outbox delete failed for {pending.key}") from exc


async def is_telegram_media_degraded(
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return whether recent photo connection failures require text-only delivery."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    try:
        response = await asyncio.to_thread(
            s3.get_object,
            Bucket=bucket_name,
            Key=TELEGRAM_MEDIA_HEALTH_KEY,
        )
        stream = response["Body"]
        try:
            payload = json.loads(stream.read())
        finally:
            stream.close()
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise StorageUnavailableError("telegram media health read failed") from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False

    raw_until = payload.get("degraded_until")
    if not isinstance(raw_until, str):
        return False
    try:
        degraded_until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    return degraded_until > (now or datetime.now(timezone.utc))


async def mark_telegram_media_degraded(
    degraded_until: datetime,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    payload = {
        "status": "text_only",
        "degraded_until": degraded_until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=bucket_name,
            Key=TELEGRAM_MEDIA_HEALTH_KEY,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as exc:
        raise StorageUnavailableError("telegram media health write failed") from exc


async def clear_telegram_media_degraded(
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    try:
        await asyncio.to_thread(
            s3.delete_object,
            Bucket=bucket_name,
            Key=TELEGRAM_MEDIA_HEALTH_KEY,
        )
    except ClientError as exc:
        raise StorageUnavailableError("telegram media health delete failed") from exc


def _is_not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _is_precondition_failed(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412", "ConditionalRequestConflict"} or status in {409, 412}


def _etag_candidates(etag: str | None) -> tuple[str, ...]:
    """Return compatible conditional-write values without weakening atomicity."""
    if not etag:
        return ()
    normalized = etag.strip()
    values = [normalized]
    unquoted = normalized.strip('"')
    if unquoted and unquoted != normalized:
        values.append(unquoted)
    if unquoted.startswith("W/"):
        strong = unquoted[2:].strip('"')
        if strong and strong not in values:
            values.extend((strong, f'"{strong}"'))
    return tuple(values)


def _object_metadata(response: dict[str, Any]) -> dict[str, Any]:
    """Return S3 user metadata with case-insensitive keys.

    S3 metadata keys are case-insensitive, but compatible endpoints and SDK
    versions may return their spelling differently. Yandex Object Storage, for
    example, can return ``Delivery-State`` for a value written as
    ``delivery-state``.
    """
    raw = response.get("Metadata") or response.get("metadata") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key).casefold(): value for key, value in raw.items()}


CLAIM_RECLAIM_MAX_ATTEMPTS = 3
CLAIM_RECLAIM_RETRY_DELAY_SECONDS = 0.05
DELIVERY_SENT_TTL_SECONDS = 24 * 60 * 60
DELIVERY_SENT_WRITE_MAX_ATTEMPTS = 3
DELIVERY_SENT_WRITE_RETRY_DELAY_SECONDS = 0.1


async def _reclaim_expired_claim(
    *,
    s3: Any,
    bucket_name: str,
    key: str,
    uid: str,
    claim_id: str,
    existing: dict[str, Any],
    reference: datetime,
    put: Any,
    event_name: str,
) -> DeliveryClaim | None:
    """Reclaim an expired lease with a fresh read after every CAS conflict."""

    current = existing
    reference_timestamp = int(reference.timestamp())
    for attempt in range(1, CLAIM_RECLAIM_MAX_ATTEMPTS + 1):
        raw_expiry = _object_metadata(current).get("expires-at")
        try:
            current_expiry = int(raw_expiry)
        except (TypeError, ValueError):
            current_expiry = int(
                (reference + timedelta(seconds=DELIVERY_CLAIM_TTL_SECONDS)).timestamp()
            )
        if current_expiry > reference_timestamp:
            logger.info(
                'event="%s" key="%s" reason="claim_owned_by_other_invocation"',
                event_name,
                key,
            )
            return None

        logger.info(
            'event="delivery_claim_reclaim_started" key="%s" expired_at="%s" attempt="%s"',
            key,
            current_expiry,
            attempt,
        )
        etags = _etag_candidates(current.get("ETag"))
        for etag in etags:
            try:
                response = await asyncio.to_thread(put, False, etag)
                logger.info(
                    'event="delivery_claim_reclaimed" key="%s" attempt="%s" etag_attempt="%s"',
                    key,
                    attempt,
                    etags.index(etag) + 1,
                )
                return DeliveryClaim(uid, key, claim_id, response.get("ETag"))
            except ClientError as exc:
                if _is_precondition_failed(exc):
                    continue
                code = str(exc.response.get("Error", {}).get("Code", "unknown"))
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                logger.error(
                    'event="delivery_claim_reclaim_failed" key="%s" code="%s" status="%s"',
                    key,
                    code,
                    status,
                )
                raise StorageUnavailableError(f"claim reclaim failed for {key}") from exc

        if attempt == CLAIM_RECLAIM_MAX_ATTEMPTS:
            logger.error(
                'event="delivery_claim_reclaim_unavailable" key="%s" reason="expired_claim_cas_failed"',
                key,
            )
            raise StorageUnavailableError(f"expired claim cannot be reclaimed for {key}")

        await asyncio.sleep(CLAIM_RECLAIM_RETRY_DELAY_SECONDS)
        try:
            current = await asyncio.to_thread(
                s3.head_object,
                Bucket=bucket_name,
                Key=key,
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise StorageUnavailableError(f"claim disappeared during reclaim for {key}") from exc
            raise StorageUnavailableError(f"claim refresh failed for {key}") from exc

    raise AssertionError("expired claim reclaim loop did not terminate")


async def claim_admin_alert(
    alert_code: str,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True once per cooldown window so repeated failures do not spam the admin."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    reference = now or datetime.now(timezone.utc)
    key = alert_key(alert_code, reference)
    body = json.dumps(
        {
            "alert_code": alert_code,
            "claimed_at": reference.isoformat().replace("+00:00", "Z"),
            "cooldown_seconds": ALERT_COOLDOWN_SECONDS,
        }
    ).encode("utf-8")

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )

    try:
        await asyncio.to_thread(_put)
        return True
    except ClientError as exc:
        if _is_precondition_failed(exc):
            return False
        raise StorageUnavailableError(f"alert claim failed for {key}") from exc


async def is_processed(match_uid: str, client: Any | None = None, bucket: str | None = None) -> bool:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = processed_key(match_uid)

    def _head() -> bool:
        try:
            s3.head_object(Bucket=bucket_name, Key=key)
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise

    try:
        return await asyncio.to_thread(_head)
    except ClientError as exc:
        logger.error('storage_error="head_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"head_object failed for {key}") from exc


async def is_match_processed(
    match: MatchNormalized,
    client: Any | None = None,
    bucket: str | None = None,
) -> bool:
    for uid in dict.fromkeys((match.match_uid, match.legacy_match_uid)):
        if await is_processed(uid, client=client, bucket=bucket):
            return True
    return False


async def is_channel_processed(
    match: MatchNormalized,
    channel_id: str,
    legacy_channel_name: str | None = None,
    client: Any | None = None,
    bucket: str | None = None,
) -> bool:
    identifiers = [channel_match_uid(match, channel_id), legacy_channel_match_uid(match, channel_id)]
    if legacy_channel_name and legacy_channel_name != channel_id:
        identifiers.extend(
            [
                channel_match_uid(match, legacy_channel_name),
                legacy_channel_match_uid(match, legacy_channel_name),
            ]
        )
    for uid in dict.fromkeys(identifiers):
        if await is_processed(uid, client=client, bucket=bucket):
            return True
    return False


async def claim_channel_delivery(
    match: MatchNormalized,
    channel_id: str,
    legacy_channel_name: str | None = None,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> DeliveryClaim | None:
    """Atomically reserve a channel delivery, reclaiming abandoned leases after TTL."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    if await is_channel_processed(
        match,
        channel_id,
        legacy_channel_name=legacy_channel_name,
        client=s3,
        bucket=bucket_name,
    ):
        return None

    uid = channel_match_uid(match, channel_id)
    key = claim_key(uid)
    reference = now or datetime.now(timezone.utc)
    expires_at = reference + timedelta(seconds=DELIVERY_CLAIM_TTL_SECONDS)
    claim_id = uuid.uuid4().hex
    body = json.dumps(
        {
            "match_uid": uid,
            "claim_id": claim_id,
            "status": "sending",
            "claimed_at": reference.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }
    ).encode("utf-8")
    metadata = {
        "expires-at": str(int(expires_at.timestamp())),
        "claim-id": claim_id,
        "delivery-state": "sending",
    }

    def _put(if_none_match: bool = False, etag: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "Bucket": bucket_name,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "Metadata": metadata,
        }
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        if etag:
            kwargs["IfMatch"] = etag
        return s3.put_object(**kwargs)

    def _head() -> dict[str, Any]:
        return s3.head_object(Bucket=bucket_name, Key=key)

    for _ in range(CLAIM_CREATE_MAX_ATTEMPTS):
        try:
            response = await asyncio.to_thread(_put, True, None)
            return DeliveryClaim(uid, key, claim_id, response.get("ETag"))
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                logger.error('storage_error="claim put failed" key="%s" error_type="%s"', key, type(exc).__name__)
                raise StorageUnavailableError(f"claim put failed for {key}") from exc
        try:
            existing = await asyncio.to_thread(_head)
        except ClientError as exc:
            if _is_not_found(exc):
                continue
            raise StorageUnavailableError(f"claim head failed for {key}") from exc

        if _object_metadata(existing).get("delivery-state") == "sent":
            return None
        return await _reclaim_expired_claim(
            s3=s3,
            bucket_name=bucket_name,
            key=key,
            uid=uid,
            claim_id=claim_id,
            existing=existing,
            reference=reference,
            put=_put,
            event_name="delivery_claim_reclaim_state",
        )
    raise StorageUnavailableError(f"claim state did not stabilize for {key}")


async def claim_content_delivery(
    content_uid: str,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> DeliveryClaim | None:
    """Atomically reserve a daily schedule or digest publication."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    uid = f"content_{safe_storage_part(content_uid)}"
    if await is_processed(uid, client=s3, bucket=bucket_name):
        return None

    key = claim_key(uid)
    reference = now or datetime.now(timezone.utc)
    expires_at = reference + timedelta(seconds=DELIVERY_CLAIM_TTL_SECONDS)
    claim_id = uuid.uuid4().hex
    body = json.dumps(
        {
            "content_uid": uid,
            "claim_id": claim_id,
            "status": "sending",
            "claimed_at": reference.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }
    ).encode("utf-8")
    metadata = {
        "expires-at": str(int(expires_at.timestamp())),
        "claim-id": claim_id,
        "delivery-state": "sending",
    }

    def _put(if_none_match: bool = False, etag: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "Bucket": bucket_name,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "Metadata": metadata,
        }
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        if etag:
            kwargs["IfMatch"] = etag
        return s3.put_object(**kwargs)

    for _ in range(CLAIM_CREATE_MAX_ATTEMPTS):
        try:
            response = await asyncio.to_thread(_put, True, None)
            return DeliveryClaim(uid, key, claim_id, response.get("ETag"))
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                raise StorageUnavailableError(f"content claim put failed for {key}") from exc
        try:
            existing = await asyncio.to_thread(s3.head_object, Bucket=bucket_name, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                continue
            raise StorageUnavailableError(f"content claim head failed for {key}") from exc

        if _object_metadata(existing).get("delivery-state") == "sent":
            return None
        return await _reclaim_expired_claim(
            s3=s3,
            bucket_name=bucket_name,
            key=key,
            uid=uid,
            claim_id=claim_id,
            existing=existing,
            reference=reference,
            put=_put,
            event_name="content_claim_reclaim_state",
        )
    raise StorageUnavailableError(f"content claim state did not stabilize for {key}")


async def release_delivery_claim(
    claim: DeliveryClaim,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    """Expire a claim after a failed send without requiring delete permissions."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    released_at = datetime.now(timezone.utc)
    payload = {
        "match_uid": claim.match_uid,
        "claim_id": claim.claim_id,
        "status": "released",
        "expires_at": released_at.isoformat().replace("+00:00", "Z"),
    }

    def _put(etag: str | None) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": bucket_name,
            "Key": claim.key,
            "Body": json.dumps(payload).encode("utf-8"),
            "ContentType": "application/json",
            "Metadata": {
                "expires-at": "0",
                "claim-id": claim.claim_id,
                "delivery-state": "released",
            },
        }
        if etag:
            kwargs["IfMatch"] = etag
        s3.put_object(**kwargs)

    for etag in _etag_candidates(claim.etag):
        try:
            await asyncio.to_thread(_put, etag)
            return
        except ClientError as exc:
            if _is_precondition_failed(exc):
                continue
            raise StorageUnavailableError(f"claim release failed for {claim.key}") from exc
    raise StorageUnavailableError(f"claim release lost ownership for {claim.key}")


async def mark_delivery_claim_sent(
    claim: DeliveryClaim,
    client: Any | None = None,
    bucket: str | None = None,
) -> DeliveryClaim:
    """Persist a confirmed Telegram delivery before writing its processed marker."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    recorded_at = datetime.now(timezone.utc)
    expires_at = recorded_at + timedelta(seconds=DELIVERY_SENT_TTL_SECONDS)
    payload = {
        "match_uid": claim.match_uid,
        "claim_id": claim.claim_id,
        "status": "sent",
        "sent_at": recorded_at.isoformat().replace("+00:00", "Z"),
    }

    def _put(etag: str) -> dict[str, Any]:
        return s3.put_object(
            Bucket=bucket_name,
            Key=claim.key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            Metadata={
                "expires-at": str(int(expires_at.timestamp())),
                "claim-id": claim.claim_id,
                "delivery-state": "sent",
            },
            IfMatch=etag,
        )

    etags = _etag_candidates(claim.etag)
    if not etags:
        raise StorageUnavailableError(f"claim sent-state is missing an ETag for {claim.key}")

    for attempt in range(1, DELIVERY_SENT_WRITE_MAX_ATTEMPTS + 1):
        retryable_error: BotoCoreError | ClientError | None = None
        for etag in etags:
            try:
                response = await asyncio.to_thread(_put, etag)
                return DeliveryClaim(claim.match_uid, claim.key, claim.claim_id, response.get("ETag"))
            except (BotoCoreError, ClientError) as exc:
                if isinstance(exc, ClientError) and _is_precondition_failed(exc):
                    continue
                retryable_error = exc
                break
        if retryable_error is None:
            break
        if attempt == DELIVERY_SENT_WRITE_MAX_ATTEMPTS:
            raise StorageUnavailableError(f"claim sent-state write failed for {claim.key}") from retryable_error
        await asyncio.sleep(DELIVERY_SENT_WRITE_RETRY_DELAY_SECONDS)
    raise StorageUnavailableError(f"claim sent-state lost ownership for {claim.key}")


async def reconcile_channel_delivery(
    match: MatchNormalized,
    channel_id: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> bool:
    """Finish a previously confirmed delivery without sending another Telegram post."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    if await is_channel_processed(match, channel_id, client=s3, bucket=bucket_name):
        return False
    try:
        existing = await asyncio.to_thread(
            s3.head_object, Bucket=bucket_name, Key=claim_key(channel_match_uid(match, channel_id))
        )
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise StorageUnavailableError("channel delivery reconciliation head failed") from exc
    if _object_metadata(existing).get("delivery-state") != "sent":
        return False
    await mark_channel_processed(match, channel_id, client=s3, bucket=bucket_name)
    return True


async def reconcile_content_delivery(
    content_uid: str,
    content_type: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> bool:
    """Finish a previously confirmed content delivery without a second send."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    uid = f"content_{safe_storage_part(content_uid)}"
    if await is_processed(uid, client=s3, bucket=bucket_name):
        return False
    try:
        existing = await asyncio.to_thread(s3.head_object, Bucket=bucket_name, Key=claim_key(uid))
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise StorageUnavailableError("content delivery reconciliation head failed") from exc
    if _object_metadata(existing).get("delivery-state") != "sent":
        return False
    await mark_content_processed(content_uid, content_type, client=s3, bucket=bucket_name)
    return True


async def mark_processed(match: MatchNormalized, client: Any | None = None, bucket: str | None = None) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = processed_key(match.match_uid)
    payload = {
        "match_uid": match.match_uid,
        "legacy_match_uid": match.legacy_match_uid,
        "source": match.source,
        "match_id": match.match_id,
        "tournament_name": match.tournament_name,
        "team1_name": match.team1_name,
        "team2_name": match.team2_name,
        "score1": match.score1,
        "score2": match.score2,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        logger.error('storage_error="put_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"put_object failed for {key}") from exc


async def mark_channel_processed(
    match: MatchNormalized,
    channel_name: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    match_uid = channel_match_uid(match, channel_name)
    key = processed_key(match_uid)
    payload = {
        "match_uid": match_uid,
        "legacy_match_uid": match.legacy_match_uid,
        "source": match.source,
        "match_id": match.match_id,
        "channel_name": channel_name,
        "tournament_name": match.tournament_name,
        "team1_name": match.team1_name,
        "team2_name": match.team2_name,
        "score1": match.score1,
        "score2": match.score2,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        logger.error('storage_error="put_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"put_object failed for {key}") from exc


async def mark_content_processed(
    content_uid: str,
    content_type: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    """Persist completion only after Telegram accepted the daily publication."""
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    uid = f"content_{safe_storage_part(content_uid)}"
    key = processed_key(uid)
    payload = {
        "content_uid": uid,
        "content_type": content_type,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        raise StorageUnavailableError(f"content put_object failed for {key}") from exc
