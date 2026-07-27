from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .config import DELIVERY_CLAIM_TTL_SECONDS, OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_ENDPOINT
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


def safe_storage_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "unknown"


def channel_match_uid(match: MatchNormalized, channel_name: str) -> str:
    return f"{safe_storage_part(channel_name)}_{match.match_uid}"


def legacy_channel_match_uid(match: MatchNormalized, channel_name: str) -> str:
    return f"{safe_storage_part(channel_name)}_{match.legacy_match_uid}"


def _is_not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _is_precondition_failed(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412", "ConditionalRequestConflict"} or status in {409, 412}


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
    metadata = {"expires-at": str(int(expires_at.timestamp())), "claim-id": claim_id}

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

    try:
        response = await asyncio.to_thread(_put, True, None)
        return DeliveryClaim(uid, key, claim_id, response.get("ETag"))
    except ClientError as exc:
        if not _is_precondition_failed(exc):
            logger.error('storage_error="claim put failed" key="%s" error_type="%s"', key, type(exc).__name__)
            raise StorageUnavailableError(f"claim put failed for {key}") from exc

    def _head() -> dict[str, Any]:
        return s3.head_object(Bucket=bucket_name, Key=key)

    try:
        existing = await asyncio.to_thread(_head)
    except ClientError as exc:
        if _is_not_found(exc):
            return await claim_channel_delivery(
                match,
                channel_id,
                legacy_channel_name=legacy_channel_name,
                client=s3,
                bucket=bucket_name,
                now=reference,
            )
        raise StorageUnavailableError(f"claim head failed for {key}") from exc

    raw_expiry = existing.get("Metadata", {}).get("expires-at")
    try:
        existing_expiry = int(raw_expiry)
    except (TypeError, ValueError):
        existing_expiry = int((reference + timedelta(seconds=DELIVERY_CLAIM_TTL_SECONDS)).timestamp())
    if existing_expiry > int(reference.timestamp()):
        return None

    try:
        response = await asyncio.to_thread(_put, False, existing.get("ETag"))
        return DeliveryClaim(uid, key, claim_id, response.get("ETag"))
    except ClientError as exc:
        if _is_precondition_failed(exc):
            return None
        raise StorageUnavailableError(f"claim reclaim failed for {key}") from exc


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

    def _put() -> None:
        kwargs: dict[str, Any] = {
            "Bucket": bucket_name,
            "Key": claim.key,
            "Body": json.dumps(payload).encode("utf-8"),
            "ContentType": "application/json",
            "Metadata": {"expires-at": "0", "claim-id": claim.claim_id},
        }
        if claim.etag:
            kwargs["IfMatch"] = claim.etag
        s3.put_object(**kwargs)

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        if not _is_precondition_failed(exc):
            raise StorageUnavailableError(f"claim release failed for {claim.key}") from exc


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
