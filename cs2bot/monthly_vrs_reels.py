"""One world VRS Reel per closed month; isolated 600-second function entrypoint."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

import requests
from botocore.exceptions import ClientError

from . import instagram_publish as instagram
from .match_sources import storage
from .publication_admin import publication_allowed, record_publication
from .reel_layout import LAYOUT_VERSION, COVER_OFFSET_MS

MOSCOW = ZoneInfo("Europe/Moscow")
CONTENT_TYPE = "monthly_vrs_reel"
MAX_VIDEO_BYTES = 64 * 1024 * 1024
logger = logging.getLogger(__name__)


def previous_month(now: datetime) -> str:
    local = now.astimezone(MOSCOW)
    return (local.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def validate_month(month: str, now: datetime) -> str:
    if not isinstance(month, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("invalid month")
    if month >= now.astimezone(MOSCOW).strftime("%Y-%m"):
        raise ValueError("month is not closed")
    return month


def _uid(month: str) -> str:
    return f"instagram_vrs_monthly_world_{month}"


def _enabled() -> bool:
    value = os.getenv("ENABLE_MONTHLY_VRS_REELS", "0").strip().lower()
    if value not in {"0", "1"}:
        raise ValueError("invalid monthly publishing flag")
    return value == "1" and instagram.instagram_publishing_enabled()


def _policy() -> tuple[bool, int]:
    # A missing/unavailable owner policy never enables this new publisher.
    return asyncio.run(publication_allowed("instagram", CONTENT_TYPE, control_enabled=True))


def _read(key: str) -> dict[str, Any] | None:
    try:
        result = storage._client().get_object(Bucket=storage._bucket(), Key=key)
    except ClientError as exc:
        if storage._is_not_found(exc):
            return None
        raise storage.StorageUnavailableError("monthly state read failed") from exc
    body = result["Body"]
    try:
        payload = json.loads(body.read(256 * 1024))
        if not isinstance(payload, dict):
            raise ValueError("invalid monthly state")
        return payload
    finally:
        body.close()


def _first_write(key: str, value: dict[str, Any]) -> dict[str, Any]:
    try:
        storage._client().put_object(Bucket=storage._bucket(), Key=key,
            Body=json.dumps(value, ensure_ascii=False).encode(), ContentType="application/json", IfNoneMatch="*")
        return value
    except ClientError as exc:
        if not storage._is_precondition_failed(exc):
            raise storage.StorageUnavailableError("monthly state write failed") from exc
        existing = _read(key)
        if existing is None:
            raise storage.StorageUnavailableError("monthly state disappeared") from exc
        return existing


def request_render(month: str, context: Any, *, dry_run: bool) -> dict[str, Any]:
    function_id = os.getenv("VRS_RENDER_FUNCTION_ID", "").strip()
    if not re.fullmatch(r"[a-z0-9]{20}", function_id):
        raise ValueError("VRS renderer is not configured")
    try:
        response = requests.post(f"https://functions.yandexcloud.net/{function_id}",
            params={"tag": "production"}, headers={"Authorization": f"Bearer {instagram._iam_token(context)}"},
            json={"job": "monthly", "month": month, "dry_run": dry_run},
            timeout=(5, 350), allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 256 * 1024:
            raise ValueError("excessive renderer response")
        result = response.json()
        if result.get("statusCode") is not None:
            if result["statusCode"] != 200:
                raise ValueError("renderer failed")
            result = json.loads(result["body"])
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("monthly renderer unavailable or source incomplete") from exc
    if not isinstance(result, dict) or result.get("month") != month or result.get("dry_run") is not dry_run:
        raise ValueError("unexpected renderer result")
    return result


def _receipt(receipt: dict[str, Any], month: str) -> dict[str, Any]:
    result = receipt.get("render", {})
    prefix = f"vrs-monthly/{month}/"
    key = result.get("artifacts", {}).get("videoKey", "")
    media = result.get("render", {})
    if (receipt.get("month") != month or type(receipt.get("generation")) is not int
        or receipt["generation"] < 0 or result.get("month") != month
        or result.get("dry_run") is not False
        or not re.fullmatch(re.escape(prefix) + r"renders/world/[a-f0-9]{64}/[a-f0-9]{64}/reel\.mp4", key)
        or not re.fullmatch(re.escape(prefix) + r"sources/[a-f0-9]{64}\.json", result.get("sourceKey", ""))
        or media.get("fullDecode") != "passed" or media.get("frames") != 1944
        or media.get("width") != 1080 or media.get("height") != 1920 or media.get("fps") != 60
        or media.get("layoutVersion") != LAYOUT_VERSION or media.get("coverOffsetMs") != COVER_OFFSET_MS
        or type(media.get("bytes")) is not int or not 0 < media["bytes"] <= MAX_VIDEO_BYTES
        or not re.fullmatch(r"[a-f0-9]{64}", media.get("sha256", ""))
        or key.split("/")[-2] != media["sha256"]
        or key.split("/")[-3] != result["sourceKey"].split("/")[-1][:-5]
        or not isinstance(result.get("caption"), str) or not result["caption"].startswith("Изменения VRS по итогам ")):
        raise ValueError("invalid monthly render receipt")
    return result


def _video(result: dict[str, Any]) -> bytes:
    response = storage._client().get_object(Bucket=storage._bucket(), Key=result["artifacts"]["videoKey"])
    body = response["Body"]
    try:
        video = body.read(MAX_VIDEO_BYTES + 1)
    finally:
        body.close()
    if len(video) != result["render"]["bytes"] or hashlib.sha256(video).hexdigest() != result["render"]["sha256"]:
        raise ValueError("monthly video checksum mismatch")
    return video


def _pending(state: dict[str, Any], month: str) -> dict[str, Any]:
    if (state.get("month") != month or type(state.get("generation")) is not int or state["generation"] < 0
        or not isinstance(state.get("container_id"), str) or not state["container_id"].isdecimal()):
        raise ValueError("invalid monthly container state")
    return state


def prepare(month: str, context: Any, generation: int) -> dict[str, Any] | None:
    pending_key = f"vrs-monthly/pending/{month}.json"
    existing = _read(pending_key)
    if existing is not None:
        return _pending(existing, month)
    claim = asyncio.run(storage.claim_content_delivery(_uid(month) + "_prepare"))
    if claim is None:
        return None
    try:
        # Recheck after taking the preparation lease: another call may have won.
        existing = _read(pending_key)
        if existing is not None:
            return _pending(existing, month)
        key = f"vrs-monthly/prepared/{month}.json"
        receipt = _read(key)
        if receipt is None:
            result = request_render(month, context, dry_run=False)
            candidate = {"month": month, "generation": generation, "render": result}
            _receipt(candidate, month)
            receipt = _first_write(key, candidate)
        result = _receipt(receipt, month)
        allowed, current = _policy()
        if not allowed or receipt["generation"] != current:
            return None
        url = instagram.upload_public_reel(_uid(month), _video(result))
        container = instagram.create_reel_container(url, result["caption"], context)
        return _pending(_first_write(pending_key, {"month": month, "generation": receipt["generation"],
            "container_id": container, "created_at": datetime.now(timezone.utc).isoformat()}), month)
    finally:
        asyncio.run(storage.release_delivery_claim(claim))


def advance(state: dict[str, Any], context: Any) -> str:
    month = state["month"]
    allowed, generation = _policy()
    if not allowed or state["generation"] != generation:
        return "held_or_stale"
    uid = _uid(month)
    if asyncio.run(storage.reconcile_content_delivery(uid, CONTENT_TYPE)):
        return "reconciled"
    claim = asyncio.run(storage.claim_content_delivery(uid))
    if claim is None:
        return "duplicate_or_blocked"
    attempting = False
    try:
        status = instagram.reel_container_status(state["container_id"], context)
        if status in {"IN_PROGRESS", "PENDING"}:
            return "processing"
        if status != "FINISHED":
            attempting = True
            asyncio.run(storage.mark_delivery_claim_uncertain(claim))
            return "processing_failed"
        allowed, generation = _policy()
        if not allowed or state["generation"] != generation:
            return "held_or_stale"
        # Set before the call: an ambiguous state write must never release a publish claim.
        attempting = True
        claim = asyncio.run(storage.mark_delivery_claim_attempting(claim))
        media_id = instagram.publish_reel_container(state["container_id"], context)
        claim = asyncio.run(storage.mark_delivery_claim_sent(claim))
        asyncio.run(storage.mark_content_processed(uid, CONTENT_TYPE))
        try:
            asyncio.run(record_publication(uid, "instagram", "instagram", CONTENT_TYPE, month,
                external_id=media_id, generation=generation, metadata={"month": month, "scope": "world"}))
        except storage.StorageUnavailableError:
            logger.error("monthly_vrs_journal_unavailable")
        return "published"
    except Exception:
        if attempting:
            try:
                asyncio.run(storage.mark_delivery_claim_uncertain(claim))
            except Exception:
                pass  # attempting remains non-reclaimable even when persistence fails.
            logger.error("monthly_vrs_publish_uncertain")
            return "publish_uncertain"
        raise
    finally:
        if not attempting:
            asyncio.run(storage.release_delivery_claim(claim))


def handler(event: dict[str, Any], context: Any, *, now: datetime | None = None) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    stage = "request"
    try:
        if isinstance(event, dict) and "messages" in event:
            messages = event["messages"]
            if len(messages) != 1:
                raise ValueError("invalid timer envelope")
            event = json.loads(messages[0]["details"]["payload"])
        elif isinstance(event, dict) and isinstance(event.get("body"), str):
            event = json.loads(event["body"])
        if not isinstance(event, dict) or event.get("job") != "monthly_vrs":
            raise ValueError("invalid job")
        dry = event.get("dry_run", True)
        if type(dry) is not bool or type(event.get("inspect_source", False)) is not bool:
            raise ValueError("invalid dry run")
        month = validate_month(event.get("month", previous_month(reference)), reference)
        if not dry and "month" in event:
            raise ValueError("historical/manual publication is not supported")
        result: dict[str, Any] = {"job": "monthly_vrs", "month": month, "scope": "world", "dry_run": dry}
        if dry:
            if event.get("inspect_source"):
                stage = "source"
                result["source"] = request_render(month, context, dry_run=True)
                stage = "policy"
                allowed, generation = _policy()
                result["readiness"] = {"publishing_enabled": _enabled(), "owner_allowed": allowed, "generation": generation}
            result["schedule"] = "1st day, 12:00 Europe/Moscow; retry window days 1–3, 12:00–15:59"
        else:
            local = reference.astimezone(MOSCOW)
            if not (1 <= local.day <= 3 and 12 <= local.hour < 16):
                result["state"] = "outside_window"
            elif not _enabled():
                result["state"] = "disabled"
            else:
                stage = "policy"
                allowed, generation = _policy()
                if not allowed:
                    result["state"] = "held_by_owner"
                else:
                    uid = _uid(month)
                    stage = "delivery"
                    done = asyncio.run(storage.is_processed(f"content_{storage.safe_storage_part(uid)}"))
                    if done:
                        result["state"] = "already_sent"
                    elif asyncio.run(storage.reconcile_content_delivery(uid, CONTENT_TYPE)):
                        result["state"] = "reconciled"
                    else:
                        state = prepare(month, context, generation)
                        result["state"] = advance(state, context) if state else "preparing_or_held"
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}
    except Exception as exc:
        logger.error("monthly_vrs_failed", extra={"stage": stage, "error_type": type(exc).__name__})
        return {"statusCode": 400 if stage == "request" else 502,
                "body": json.dumps({"error": "monthly_vrs_failed", "stage": stage})}
