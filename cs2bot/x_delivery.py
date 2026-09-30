"""Fail-closed orchestration for X posts created and checked through Buffer."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from . import buffer_x_publish
from .match_sources.storage import (
    XDeliveryLease,
    XDeliveryRecord,
    claim_x_delivery_create,
    get_x_delivery,
    list_due_x_deliveries,
    mark_x_delivery_accepted,
    mark_x_delivery_sent,
    mark_x_delivery_uncertain,
    prepare_x_delivery,
    reschedule_x_delivery_check,
    write_analytics_record,
)

logger = logging.getLogger(__name__)

X_DELIVERY_CHECK_INTERVAL_SECONDS = 5 * 60
MAX_BUFFER_CHECKS_PER_RUN = 3
MAX_RECOVERY_POSTS = 50
RECOVERY_TIME_BEFORE_SECONDS = 30
RECOVERY_TIME_AFTER_SECONDS = 5 * 60


def _as_utc(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_x_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def _asset_sources(post: dict[str, Any]) -> list[str]:
    assets = post.get("assets")
    if not isinstance(assets, list):
        return []
    return [
        asset["source"]
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("source"), str)
    ]


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _exact_recovery_matches(
    record: XDeliveryRecord,
    posts: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    expected_urls = list(record.image_urls)
    if not expected_urls or len(set(expected_urls)) != len(expected_urls):
        return []
    expected = set(expected_urls)
    matches: list[dict[str, Any]] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        if post.get("channelId") != record.channel_id or post.get("text") != record.text.strip():
            continue
        created_at = _parse_aware_datetime(post.get("createdAt"))
        if created_at is None or not window_start <= created_at <= window_end:
            continue
        sources = _asset_sources(post)
        if len(sources) == len(expected_urls) and len(set(sources)) == len(sources) and set(sources) == expected:
            matches.append(post)
    return matches


async def _diagnostic(
    record: XDeliveryRecord,
    event: str,
    *,
    details: dict[str, Any] | None = None,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> None:
    occurred_at = _timestamp(now)
    event_id = hashlib.sha256(
        f"{record.content_uid}:{event}:{occurred_at}".encode("utf-8")
    ).hexdigest()
    try:
        await write_analytics_record(
            "x_delivery_recovery",
            event_id,
            {
                "event": event,
                "content_uid": record.content_uid,
                "status": record.status,
                "occurred_at": occurred_at,
                "details": details or {},
            },
            client=client,
            bucket=bucket,
        )
    except Exception as exc:  # Diagnostic failure must not reopen or mask the delivery state.
        logger.error(
            'event="x_delivery_diagnostic_write_failed" content_uid="%s" error_type="%s"',
            record.content_uid,
            type(exc).__name__,
        )


async def _uncertain(
    lease: XDeliveryLease,
    event: str,
    *,
    details: dict[str, Any] | None = None,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> XDeliveryLease:
    uncertain = await mark_x_delivery_uncertain(lease, client=client, bucket=bucket, now=now)
    await _diagnostic(
        uncertain.record,
        event,
        details=details,
        client=client,
        bucket=bucket,
        now=now,
    )
    return uncertain


async def _accept_or_send_recovered(
    lease: XDeliveryLease,
    post: dict[str, Any],
    *,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> XDeliveryLease:
    post_id = post.get("id")
    if not isinstance(post_id, str) or not post_id:
        return await _uncertain(
            lease,
            "recovery_match_missing_buffer_id",
            client=client,
            bucket=bucket,
            now=now,
        )
    next_check = _timestamp(now + timedelta(seconds=X_DELIVERY_CHECK_INTERVAL_SECONDS))
    accepted = await mark_x_delivery_accepted(
        lease,
        post_id,
        next_check,
        client=client,
        bucket=bucket,
        now=now,
    )
    if str(post.get("status", "")).lower() == "sent" and _is_x_url(post.get("externalLink")):
        return await mark_x_delivery_sent(
            accepted,
            post["externalLink"],
            client=client,
            bucket=bucket,
            now=now,
        )
    if str(post.get("status", "")).lower() == "sent":
        await _diagnostic(
            accepted.record,
            "sent_post_missing_x_link",
            client=client,
            bucket=bucket,
            now=now,
        )
    return accepted


async def _reconcile_attempting(
    lease: XDeliveryLease,
    api_key: str,
    organization_id: str,
    *,
    buffer_api: Any,
    http_client: Any,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> XDeliveryLease:
    record = lease.record
    attempted_at = _parse_aware_datetime(record.attempting_at)
    if not record.channel_id or attempted_at is None:
        return await _uncertain(
            lease,
            "recovery_missing_channel_or_attempt_time",
            client=client,
            bucket=bucket,
            now=now,
        )
    try:
        posts = await asyncio.to_thread(
            buffer_api.find_recent_channel_posts,
            api_key,
            organization_id,
            record.channel_id,
            attempted_at - timedelta(seconds=RECOVERY_TIME_BEFORE_SECONDS),
            created_before=attempted_at + timedelta(seconds=RECOVERY_TIME_AFTER_SECONDS),
            first=MAX_RECOVERY_POSTS,
            max_pages=1,
            http_client=http_client,
        )
    except Exception as exc:
        return await _uncertain(
            lease,
            "recent_post_lookup_failed",
            details={"error_type": type(exc).__name__},
            client=client,
            bucket=bucket,
            now=now,
        )
    if getattr(posts, "truncated", False):
        return await _uncertain(
            lease,
            "recent_post_lookup_truncated",
            details={"returned_count": len(posts)},
            client=client,
            bucket=bucket,
            now=now,
        )
    window_start = attempted_at - timedelta(seconds=RECOVERY_TIME_BEFORE_SECONDS)
    window_end = attempted_at + timedelta(seconds=RECOVERY_TIME_AFTER_SECONDS)
    matches = _exact_recovery_matches(record, posts, window_start, window_end)
    if len(matches) != 1:
        return await _uncertain(
            lease,
            "recent_post_match_not_unique",
            details={"match_count": len(matches)},
            client=client,
            bucket=bucket,
            now=now,
        )
    return await _accept_or_send_recovered(
        lease,
        matches[0],
        client=client,
        bucket=bucket,
        now=now,
    )


async def create_x_delivery(
    content_uid: str,
    channel_id: str,
    organization_id: str,
    text: str,
    image_urls: list[str] | tuple[str, ...],
    api_key: str,
    *,
    client: Any | None = None,
    bucket: str | None = None,
    http_client: Any = requests,
    buffer_api: Any = buffer_x_publish,
    now: datetime | None = None,
) -> XDeliveryLease:
    """Prepare and create once; existing non-prepared state can never create again."""
    moment = _as_utc(now)
    await prepare_x_delivery(
        content_uid,
        text,
        image_urls,
        client=client,
        bucket=bucket,
        now=moment,
        channel_id=channel_id,
    )
    lease = await claim_x_delivery_create(
        content_uid,
        client=client,
        bucket=bucket,
        now=moment,
        channel_id=channel_id,
    )
    if lease is None:
        existing = await get_x_delivery(
            content_uid,
            client=client,
            bucket=bucket,
            channel_id=channel_id,
        )
        if existing is None:
            raise RuntimeError("prepared X delivery disappeared")
        return existing
    try:
        post = await asyncio.to_thread(
            buffer_api.create_x_post,
            api_key,
            channel_id,
            text,
            image_urls,
            http_client=http_client,
        )
        post_id = post.get("id") if isinstance(post, dict) else None
        if not isinstance(post_id, str) or not post_id:
            raise buffer_x_publish.BufferXDeliveryUncertainError(
                "Buffer createPost returned no post ID"
            )
    except Exception as exc:
        # The CAS already made this record non-retryable. Search once for an
        # exact match; a missing or ambiguous result is persisted as uncertain.
        await _diagnostic(
            lease.record,
            "create_post_result_ambiguous",
            details={"error_type": type(exc).__name__},
            client=client,
            bucket=bucket,
            now=moment,
        )
        return await _reconcile_attempting(
            lease,
            api_key,
            organization_id,
            buffer_api=buffer_api,
            http_client=http_client,
            client=client,
            bucket=bucket,
            now=moment,
        )
    return await mark_x_delivery_accepted(
        lease,
        post_id,
        _timestamp(moment + timedelta(seconds=X_DELIVERY_CHECK_INTERVAL_SECONDS)),
        client=client,
        bucket=bucket,
        now=moment,
    )


async def _check_accepted(
    lease: XDeliveryLease,
    api_key: str,
    *,
    buffer_api: Any,
    http_client: Any,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> XDeliveryLease:
    record = lease.record
    assert record.buffer_post_id is not None
    try:
        post = await asyncio.to_thread(
            buffer_api.get_post,
            api_key,
            record.buffer_post_id,
            http_client=http_client,
        )
        if not isinstance(post, dict):
            raise buffer_x_publish.BufferXResponseError("Buffer returned an invalid post")
    except Exception as exc:
        rescheduled = await reschedule_x_delivery_check(
            lease,
            _timestamp(now + timedelta(seconds=X_DELIVERY_CHECK_INTERVAL_SECONDS)),
            client=client,
            bucket=bucket,
            now=now,
        )
        await _diagnostic(
            rescheduled.record,
            "buffer_status_check_failed",
            details={"error_type": type(exc).__name__},
            client=client,
            bucket=bucket,
            now=now,
        )
        return rescheduled
    if post.get("id") != record.buffer_post_id or (
        record.channel_id and post.get("channelId") != record.channel_id
    ):
        return await _reschedule_with_diagnostic(
            lease,
            "buffer_post_identity_mismatch",
            client=client,
            bucket=bucket,
            now=now,
        )
    if str(post.get("status", "")).lower() == "sent":
        if _is_x_url(post.get("externalLink")):
            return await mark_x_delivery_sent(
                lease,
                post["externalLink"],
                client=client,
                bucket=bucket,
                now=now,
            )
        return await _reschedule_with_diagnostic(
            lease,
            "sent_post_missing_x_link",
            client=client,
            bucket=bucket,
            now=now,
        )
    return await reschedule_x_delivery_check(
        lease,
        _timestamp(now + timedelta(seconds=X_DELIVERY_CHECK_INTERVAL_SECONDS)),
        client=client,
        bucket=bucket,
        now=now,
    )


async def _reschedule_with_diagnostic(
    lease: XDeliveryLease,
    event: str,
    *,
    client: Any | None,
    bucket: str | None,
    now: datetime,
) -> XDeliveryLease:
    updated = await reschedule_x_delivery_check(
        lease,
        _timestamp(now + timedelta(seconds=X_DELIVERY_CHECK_INTERVAL_SECONDS)),
        client=client,
        bucket=bucket,
        now=now,
    )
    await _diagnostic(updated.record, event, client=client, bucket=bucket, now=now)
    return updated


async def check_due_x_deliveries(
    api_key: str,
    organization_id: str,
    *,
    client: Any | None = None,
    bucket: str | None = None,
    http_client: Any = requests,
    buffer_api: Any = buffer_x_publish,
    now: datetime | None = None,
    max_requests: int = MAX_BUFFER_CHECKS_PER_RUN,
) -> list[XDeliveryLease]:
    """Check only due records, with at most one bounded Buffer call per record."""
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= 10:
        raise ValueError("max_requests must be between 1 and 10")
    moment = _as_utc(now)
    due = await list_due_x_deliveries(
        moment,
        limit=max_requests,
        client=client,
        bucket=bucket,
    )
    outcomes: list[XDeliveryLease] = []
    for lease in due:
        if lease.record.status == "accepted":
            result = await _check_accepted(
                lease,
                api_key,
                buffer_api=buffer_api,
                http_client=http_client,
                client=client,
                bucket=bucket,
                now=moment,
            )
        else:
            result = await _reconcile_attempting(
                lease,
                api_key,
                organization_id,
                buffer_api=buffer_api,
                http_client=http_client,
                client=client,
                bucket=bucket,
                now=moment,
            )
        outcomes.append(result)
    return outcomes
