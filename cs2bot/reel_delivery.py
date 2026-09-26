"""Durable handoff between Reel encoding and asynchronous Meta processing."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from .instagram_publish import (
    InstagramDeliveryUncertainError,
    InstagramPublishError,
    publish_reel_container,
    reel_container_status,
)
from .match_sources import storage


@dataclass(frozen=True)
class PendingReel:
    day_key: str
    container_id: str
    match_count: int
    created_at: str

    @property
    def content_uid(self) -> str:
        return f"instagram_schedule_reel_{self.day_key}"


def _key(day_key: str) -> str:
    try:
        date.fromisoformat(day_key)
    except ValueError as exc:
        raise ValueError("invalid Reel day") from exc
    return f"reels/{day_key}.json"


def load_pending_reel(day_key: str, *, client: Any | None = None, bucket: str | None = None) -> PendingReel | None:
    s3 = client or storage._client()
    bucket_name = bucket or storage._bucket()
    try:
        response = s3.get_object(Bucket=bucket_name, Key=_key(day_key))
    except ClientError as exc:
        if storage._is_not_found(exc):
            return None
        raise storage.StorageUnavailableError("Reel state read failed") from exc
    body = response["Body"]
    try:
        payload = json.loads(body.read())
        state = PendingReel(
            day_key=payload["day_key"],
            container_id=payload["container_id"],
            match_count=payload["match_count"],
            created_at=payload["created_at"],
        )
        if (
            state.day_key != day_key
            or not isinstance(state.container_id, str)
            or not state.container_id.isdecimal()
            or not isinstance(state.match_count, int)
            or isinstance(state.match_count, bool)
            or not 1 <= state.match_count <= 20
            or not isinstance(state.created_at, str)
        ):
            raise ValueError("invalid Reel state")
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise storage.StorageUnavailableError("Reel state is invalid") from exc
    finally:
        body.close()


def save_pending_reel(
    day_key: str, container_id: str, match_count: int,
    *, client: Any | None = None, bucket: str | None = None,
) -> PendingReel:
    """The first persisted container wins; later invocations never publish theirs."""
    if not container_id.isdecimal() or not 1 <= match_count <= 20:
        raise ValueError("invalid Reel container state")
    state = PendingReel(day_key, container_id, match_count, datetime.now(timezone.utc).isoformat())
    s3 = client or storage._client()
    bucket_name = bucket or storage._bucket()
    try:
        s3.put_object(
            Bucket=bucket_name,
            Key=_key(day_key),
            Body=json.dumps(state.__dict__).encode("utf-8"),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return state
    except ClientError as exc:
        if storage._is_precondition_failed(exc):
            existing = load_pending_reel(day_key, client=s3, bucket=bucket_name)
            if existing is None:
                raise storage.StorageUnavailableError("Reel state disappeared after conflict") from exc
            return existing
        raise storage.StorageUnavailableError("Reel state write failed") from exc


def advance_pending_reel(state: PendingReel, context: Any) -> str:
    """Advance one persisted container safely; never retry an ambiguous publish."""
    if asyncio.run(storage.reconcile_content_delivery(state.content_uid, "schedule_reel")):
        return "reconciled"
    claim = asyncio.run(storage.claim_content_delivery(state.content_uid))
    if claim is None:
        return "duplicate_or_blocked"
    attempting = False
    try:
        status = reel_container_status(state.container_id, context)
        if status in {"IN_PROGRESS", "PENDING"}:
            asyncio.run(storage.release_delivery_claim(claim))
            return "processing"
        if status != "FINISHED":
            asyncio.run(storage.mark_delivery_claim_uncertain(claim))
            return "processing_failed"
        claim = asyncio.run(storage.mark_delivery_claim_attempting(claim))
        attempting = True
        publish_reel_container(state.container_id, context)
        claim = asyncio.run(storage.mark_delivery_claim_sent(claim))
        asyncio.run(storage.mark_content_processed(state.content_uid, "schedule_reel"))
        return "published"
    except (InstagramDeliveryUncertainError, InstagramPublishError):
        if attempting:
            try:
                asyncio.run(storage.mark_delivery_claim_uncertain(claim))
            except storage.StorageUnavailableError:
                pass  # attempting is already non-reclaimable
            return "publish_uncertain"
        asyncio.run(storage.release_delivery_claim(claim))
        return "status_unavailable"
    except Exception:
        # If a crash or state-write error follows media_publish, attempting
        # remains non-reclaimable. Before that boundary, a retry is safe.
        if not attempting:
            try:
                asyncio.run(storage.release_delivery_claim(claim))
            except storage.StorageUnavailableError:
                pass
        raise


def advance_today_reel(context: Any, now: datetime | None = None) -> dict[str, str]:
    """The existing five-minute worker never publishes a stale day's schedule."""
    from zoneinfo import ZoneInfo

    local_date = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Europe/Moscow")).date()
    state = load_pending_reel(local_date.isoformat())
    if state is None:
        return {}
    return {local_date.isoformat(): advance_pending_reel(state, context)}
