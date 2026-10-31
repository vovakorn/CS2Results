"""Capture pre-event VRS independently of editorial previews and social delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from .match_sources import storage
from .match_sources.sources.tournament_preview_source import fetch_preview_calendar
from .match_sources.sources.vrs_source import fetch_snapshot
from .match_sources.vrs import VRSDataError

logger = logging.getLogger(__name__)
REPORT_PREFIX = "diagnostics/vrs-baselines/"


def calendar_baseline_id(page: str) -> str:
    """Exact LP identity, including separators; no name matching or stage joins."""
    return "liquipedia-page:" + hashlib.sha256(page.encode("utf-8")).hexdigest()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise VRSDataError("invalid baseline timestamp") from exc
    if parsed.tzinfo is None:
        raise VRSDataError("baseline timestamp must include a timezone")
    return parsed


def validate_before(snapshot, start: datetime, *, now: datetime | None = None) -> None:
    """A late capture must never be relabelled as a pre-event observation."""
    for value in (snapshot.effective_at, snapshot.fetched_at):
        timestamp = _timestamp(value)
        if timestamp >= start:
            raise VRSDataError("initial VRS snapshot must be captured before the tournament")
        if now is not None and timestamp > now + timedelta(minutes=5):
            raise VRSDataError("initial VRS snapshot is in the future")


def _problem_signature(body):
    return (body.get("status"), body.get("error_type"), body.get("issues"), sorted(
        (row["pagename"], row["status"], row.get("error_type"))
        for row in body.get("events", []) if row["status"] in {"retry", "missed_start"}))


async def _save_report(body):
    s3, bucket = storage._client(), storage._bucket()
    previous = {}
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=bucket, Key=REPORT_PREFIX + "latest.json")
        stream = response["Body"]
        try:
            previous = json.loads(stream.read())
        finally:
            stream.close()
    except ClientError as exc:
        if not storage._is_not_found(exc):
            raise
    # Per-event facts survive when an event leaves the seven-day scan window.
    records = [("latest.json", body)] + [
        (f"events/{calendar_baseline_id(item['pagename'])}.json",
         {"checked_at": body["checked_at"], **item}) for item in body["events"]
    ]
    for suffix, record in records:
        await asyncio.to_thread(s3.put_object, Bucket=bucket,
            Key=REPORT_PREFIX + suffix,
            Body=json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            ContentType="application/json")
    return _problem_signature(previous) != _problem_signature(body)


def run_vrs_baseline_job(dry_run: bool, runtime, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    body = {"job": "vrs_baselines", "dry_run": dry_run,
            "checked_at": now.isoformat(), "events": [], "issues": []}
    if not getattr(runtime, "ENABLE_VRS", False):
        body["status"] = "disabled"
        return {"statusCode": 200, "body": json.dumps(body)}
    try:
        scan = asyncio.run(fetch_preview_calendar(now, require_prize_pool=False))
    except Exception as exc:
        body.update(status="source_unavailable", error_type=type(exc).__name__)
        scan = None
    snapshot, snapshot_error, fetched = None, None, False
    if scan is not None:
        body["issues"] = scan.row_issues
        for event in scan.events:
            page = event["pagename"]
            key = calendar_baseline_id(page)
            item = {**event, "baseline_id": key}
            try:
                start = datetime.combine(datetime.fromisoformat(event["start"]).date(),
                                         time.min, tzinfo=ZoneInfo("Europe/Moscow"))
                existing = asyncio.run(storage.read_vrs_snapshot(key, "before", "initial"))
                if existing is not None:
                    validate_before(existing, start, now=now)
                    item.update(status="existing", version=existing.version,
                                effective_at=existing.effective_at, fetched_at=existing.fetched_at)
                elif now >= start:
                    item["status"] = "missed_start"
                else:
                    # One source read per invocation, even with many events or an outage.
                    if not fetched:
                        fetched = True
                        try:
                            snapshot = asyncio.run(fetch_snapshot())
                        except Exception as exc:
                            snapshot_error = type(exc).__name__
                    if snapshot is None:
                        item.update(status="retry", error_type=snapshot_error)
                    else:
                        validate_before(snapshot, start, now=now)
                        if dry_run:
                            item.update(status="would_capture", version=snapshot.version,
                                        effective_at=snapshot.effective_at, fetched_at=snapshot.fetched_at)
                        else:
                            created = asyncio.run(storage.write_vrs_snapshot(
                                key, "before", snapshot, initial=True))
                            saved = asyncio.run(storage.read_vrs_snapshot(key, "before", "initial"))
                            if saved is None:
                                raise storage.StorageUnavailableError("baseline write was not confirmed")
                            validate_before(saved, start, now=now)
                            item.update(status="saved" if created else "existing", version=saved.version,
                                        effective_at=saved.effective_at, fetched_at=saved.fetched_at)
            except Exception as exc:
                item.update(status="retry", error_type=type(exc).__name__)
            body["events"].append(item)
        body["status"] = "gaps" if body["issues"] or any(
            item["status"] in {"retry", "missed_start"} for item in body["events"]) else "ok"
    if not dry_run:
        changed = True
        try:
            changed = asyncio.run(_save_report(body))
        except Exception as exc:
            body.update(status="report_unavailable", report_error_type=type(exc).__name__)
        if body["status"] != "ok" and changed:
            runtime._notify_admin("vrs_baselines_not_ready",
                "Не завершена подготовка начальных VRS. Проверьте diagnostics/vrs-baselines/latest.json; "
                "повторите vrs_baselines до старта турниров. Поздний снимок не заменяет пропущенный.")
    logger.info("vrs_baselines_complete status=%s events=%s dry_run=%s",
                body["status"], len(body["events"]), dry_run)
    return {"statusCode": 200 if body["status"] == "ok" else 503,
            "body": json.dumps(body, ensure_ascii=False)}
