"""Best-effort product analytics stored alongside delivery state."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .match_sources.storage import write_analytics_record


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def record_post(
    channel_id: str,
    content_uid: str,
    job: str,
    *,
    message_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    payload = {
        "recorded_at": _timestamp(),
        "channel_id": channel_id,
        "content_uid": content_uid,
        "job": job,
        "message_id": message_id,
        "metadata": metadata or {},
    }
    return await write_analytics_record("posts", f"{channel_id}/{content_uid}", payload)


async def record_subscriber_snapshot(channel_id: str, member_count: int) -> str:
    captured_at = _timestamp()
    return await write_analytics_record(
        "subscribers",
        f"{channel_id}/{captured_at}",
        {"captured_at": captured_at, "channel_id": channel_id, "member_count": member_count},
    )


async def record_campaign_invite(channel_id: str, campaign: str, invite_link: str) -> str:
    return await write_analytics_record(
        "invites",
        f"{channel_id}/{campaign}",
        {"created_at": _timestamp(), "channel_id": channel_id, "campaign": campaign, "invite_link": invite_link},
    )


async def record_manual_post_metrics(
    channel_id: str,
    message_id: int,
    views_24h: int | None,
    reactions: int | None,
) -> str:
    captured_at = _timestamp()
    return await write_analytics_record(
        "post_metrics",
        f"{channel_id}/{message_id}/{captured_at}",
        {"captured_at": captured_at, "channel_id": channel_id, "message_id": message_id, "views_24h": views_24h, "reactions": reactions},
    )
