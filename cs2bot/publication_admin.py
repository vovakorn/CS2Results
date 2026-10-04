"""Private publication policy and auditable journal for the Telegram admin bot.

This module never sends content.  It only stores an owner policy and facts which
were already confirmed by a publisher.  Keeping it separate from delivery
claims makes it impossible for the UI to reopen an uncertain delivery.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from .match_sources.storage import StorageUnavailableError, _bucket, _client, _is_not_found, _is_precondition_failed, safe_storage_part

POLICY_KEY = "admin/publication-policy-v1.json"
JOURNAL_PREFIX = "admin/publications/"
POLICY_VERSION = 1
MOSCOW = ZoneInfo("Europe/Moscow")

PUBLICATION_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "tournament_preview": ("Перед стартом турнира", ("telegram", "instagram", "threads")),
    "schedule": ("Расписание матчей", ("telegram", "instagram", "threads")),
    "schedule_context": ("Форма и очные встречи", ("telegram",)),
    "results": ("Результаты матчей", ("telegram", "instagram", "threads")),
    "digest": ("Итог дня", ("telegram", "instagram", "threads")),
    "radar": ("Турнирный радар / сетка", ("telegram", "threads")),
    "tournament_standings": ("Итоги турнира", ("telegram", "instagram", "threads")),
    "tournament_vrs_standings": ("Изменения VRS", ("telegram", "instagram", "threads")),
    "schedule_reel": ("Reel расписания", ("instagram",)),
}


@dataclass(frozen=True)
class PolicySnapshot:
    revision: int
    entries: dict[str, dict[str, Any]]
    etag: str | None


def policy_entry_id(destination_id: str, publication_type: str) -> str:
    if publication_type not in PUBLICATION_TYPES:
        raise ValueError("unknown publication type")
    if not destination_id or len(destination_id) > 100:
        raise ValueError("invalid destination")
    return f"{destination_id}:{publication_type}"


def _default_policy() -> PolicySnapshot:
    return PolicySnapshot(revision=0, entries={}, etag=None)


def _decode_policy(body: bytes, etag: str | None) -> PolicySnapshot:
    try:
        payload = json.loads(body)
        if payload.get("version") != POLICY_VERSION or not isinstance(payload.get("revision"), int):
            raise ValueError
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise ValueError
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError
            if not isinstance(value.get("enabled"), bool) or not isinstance(value.get("generation"), int):
                raise ValueError
        return PolicySnapshot(payload["revision"], entries, etag)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StorageUnavailableError("publication policy is invalid") from exc


async def read_policy(*, client: Any | None = None, bucket: str | None = None) -> PolicySnapshot:
    s3, bucket_name = client or _client(), bucket or _bucket()
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=bucket_name, Key=POLICY_KEY)
    except ClientError as exc:
        if _is_not_found(exc):
            return _default_policy()
        raise StorageUnavailableError("publication policy read failed") from exc
    body = response["Body"]
    try:
        return _decode_policy(body.read(), response.get("ETag"))
    finally:
        body.close()


async def update_policy(
    destination_id: str,
    publication_type: str,
    enabled: bool,
    *,
    actor_user_id: int,
    operation_id: str,
    expected_revision: int,
    client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> PolicySnapshot:
    """CAS update.  Disabling advances the queue generation exactly once."""
    if not isinstance(actor_user_id, int) or actor_user_id <= 0 or not operation_id:
        raise ValueError("invalid policy actor or operation")
    key = policy_entry_id(destination_id, publication_type)
    current = await read_policy(client=client, bucket=bucket)
    if current.revision != expected_revision:
        raise ValueError("stale_policy_revision")
    entries = dict(current.entries)
    previous = entries.get(key, {"enabled": True, "generation": 0})
    generation = int(previous["generation"])
    if previous["enabled"] and not enabled:
        generation += 1
    timestamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    entries[key] = {"enabled": enabled, "generation": generation, "changed_at": timestamp,
                    "changed_by": actor_user_id, "operation_id": operation_id}
    payload = {"version": POLICY_VERSION, "revision": current.revision + 1, "entries": entries}
    s3, bucket_name = client or _client(), bucket or _bucket()
    kwargs: dict[str, Any] = {"Bucket": bucket_name, "Key": POLICY_KEY,
                               "Body": json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
                               "ContentType": "application/json"}
    if current.etag:
        kwargs["IfMatch"] = current.etag
    else:
        kwargs["IfNoneMatch"] = "*"
    try:
        response = await asyncio.to_thread(s3.put_object, **kwargs)
    except ClientError as exc:
        if _is_precondition_failed(exc):
            raise ValueError("stale_policy_revision") from exc
        raise StorageUnavailableError("publication policy update failed") from exc
    return PolicySnapshot(payload["revision"], entries, response.get("ETag"))


def policy_status(snapshot: PolicySnapshot, destination_id: str, publication_type: str) -> tuple[bool, int]:
    entry = snapshot.entries.get(policy_entry_id(destination_id, publication_type))
    return (True, 0) if entry is None else (bool(entry["enabled"]), int(entry["generation"]))


async def publication_allowed(destination_id: str, publication_type: str, *, control_enabled: bool, client: Any | None = None, bucket: str | None = None) -> tuple[bool, int]:
    """Fail closed only after the new control feature is explicitly enabled."""
    if not control_enabled:
        return True, 0
    snapshot = await read_policy(client=client, bucket=bucket)
    # Enabling the control plane before its initial policy is written must not
    # silently reopen every publisher.  A real policy always contains at least
    # the entry being changed, so the empty revision-zero document is the
    # uninitialised state.
    if snapshot.revision == 0 and not snapshot.entries:
        return False, 0
    return policy_status(snapshot, destination_id, publication_type)


def _journal_key(publication_id: str, confirmed_at: datetime) -> str:
    return f"{JOURNAL_PREFIX}{confirmed_at.date().isoformat()}/{safe_storage_part(publication_id)}.json"


async def record_publication(
    publication_id: str, destination_id: str, platform: str, publication_type: str, source_key: str,
    *, external_id: str | None = None, generation: int = 0, metadata: dict[str, Any] | None = None,
    test: bool = False, client: Any | None = None, bucket: str | None = None, now: datetime | None = None,
) -> bool:
    """Idempotently persist only a confirmed publication fact."""
    if platform not in {"telegram", "instagram", "threads"} or publication_type not in PUBLICATION_TYPES:
        raise ValueError("invalid publication")
    confirmed = now or datetime.now(timezone.utc)
    payload = {"version": 1, "publication_id": publication_id, "destination_id": destination_id,
               "platform": platform, "publication_type": publication_type, "source_key": source_key,
               "confirmed_at": confirmed.isoformat().replace("+00:00", "Z"), "external_id": external_id,
               "generation": generation, "metadata": metadata or {}, "test": test, "status": "sent"}
    s3, bucket_name = client or _client(), bucket or _bucket()
    try:
        await asyncio.to_thread(s3.put_object, Bucket=bucket_name, Key=_journal_key(publication_id, confirmed),
                                Body=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
                                ContentType="application/json", IfNoneMatch="*")
        return True
    except ClientError as exc:
        if _is_precondition_failed(exc):
            return False
        raise StorageUnavailableError("publication journal write failed") from exc


async def list_publications(*, client: Any | None = None, bucket: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    s3, bucket_name = client or _client(), bucket or _bucket()
    keys: list[str] = []; token: str | None = None
    while len(keys) < limit:
        kwargs: dict[str, Any] = {"Bucket": bucket_name, "Prefix": JOURNAL_PREFIX, "MaxKeys": min(1000, limit - len(keys))}
        if token: kwargs["ContinuationToken"] = token
        try: page = await asyncio.to_thread(s3.list_objects_v2, **kwargs)
        except ClientError as exc: raise StorageUnavailableError("publication journal list failed") from exc
        keys.extend(item["Key"] for item in page.get("Contents", []) if isinstance(item.get("Key"), str))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token: break
    result: dict[str, dict[str, Any]] = {}
    for key in keys:
        try: response = await asyncio.to_thread(s3.get_object, Bucket=bucket_name, Key=key)
        except ClientError: continue
        body = response["Body"]
        try:
            item = json.loads(body.read())
            if isinstance(item, dict) and isinstance(item.get("publication_id"), str): result[item["publication_id"]] = item
        except (ValueError, TypeError, json.JSONDecodeError): pass
        finally: body.close()
    return sorted(result.values(), key=lambda item: str(item.get("confirmed_at", "")), reverse=True)


def period_start(period: str, *, now: datetime | None = None) -> datetime:
    """Return a Moscow calendar boundary while journal timestamps remain UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    if period == "today":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return (local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("invalid period")


def publications_in_period(records: Iterable[dict[str, Any]], period: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    start = period_start(period, now=now)
    visible: list[dict[str, Any]] = []
    for record in records:
        if record.get("test"):
            continue
        raw = record.get("confirmed_at")
        if not isinstance(raw, str):
            continue
        try:
            confirmed = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(MOSCOW)
        except ValueError:
            continue
        if confirmed >= start:
            visible.append(record)
    return visible


def pending_delivery_status(pending: Any, snapshot: PolicySnapshot) -> str:
    """Classify an existing outbox item without changing its claim or queue."""
    publication_type = {"result": "results"}.get(str(getattr(pending, "content_type", "result")), str(getattr(pending, "content_type", "result")))
    enabled, generation = policy_status(snapshot, str(getattr(pending, "channel_id", "")), publication_type)
    item_generation = int(getattr(pending, "generation", 0))
    if item_generation < generation:
        return "manual_review" if enabled else "suspended"
    if not enabled:
        return "suspended"
    return "retry_safe" if int(getattr(pending, "attempt_count", 0)) else "pending"


def queue_counts(pending: Iterable[Any], snapshot: PolicySnapshot) -> dict[str, int]:
    result = {"pending": 0, "retry_safe": 0, "suspended": 0, "manual_review": 0}
    for item in pending:
        status = pending_delivery_status(item, snapshot)
        result[status] = result.get(status, 0) + 1
    return result
