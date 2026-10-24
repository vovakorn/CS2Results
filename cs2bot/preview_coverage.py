"""Read-only calendar audit; never creates editorial profiles or event identity."""
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from .match_sources.sources.tournament_preview_source import fetch_preview_calendar
from .tournament_preview import PreviewUnavailable, check_profile

logger = logging.getLogger(__name__)


def audit_preview_coverage(profiles, now, runtime, *, dry_run):
    """Report missing/stale/different-date profiles without changing publication."""
    try:
        scan = asyncio.run(fetch_preview_calendar(now))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PreviewUnavailable) else type(exc).__name__
        if isinstance(exc, PreviewUnavailable):
            logger.warning("preview_coverage_unavailable reason=%s", reason)
        else:
            logger.error("preview_coverage_unavailable reason=%s", reason,
                exc_info=(RuntimeError, RuntimeError("calendar error details redacted"), exc.__traceback__))
        if not dry_run:
            runtime._notify_admin("preview_coverage_unavailable",
                "Не проверено покрытие предтурнирных анонсов: календарь недоступен. "
                "Проверенные профили обрабатываются отдельно.")
        return {"status": "unavailable", "reason": reason, "issues": []}
    events = scan.events
    if scan.row_issues:
        logger.warning("preview_coverage_partial invalid_rows=%s", len(scan.row_issues))
        if not dry_run:
            runtime._notify_admin("preview_coverage_partial",
                "Календарь анонсов проверен частично: есть неполные записи турниров. "
                "Остальные события проверены; нужна проверка данных источника.")
    today = now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
    by_page = {profile.liquipedia_page: profile for profile in profiles}
    issues = []
    for event in events:
        profile = by_page.get(event["pagename"])
        reason = None
        if profile is None:
            reason = "preview_profile_missing"
        elif (min(stage.start for stage in profile.stages).isoformat() != event["start"]
                or max(stage.end for stage in profile.stages).isoformat() != event["end"]):
            reason = "preview_profile_dates_conflict"
        elif event["start"] > today:
            try:
                check_profile(profile, now)
            except PreviewUnavailable as exc:
                reason = str(exc)
        if reason:
            issues.append({**event, "reason": reason})
            logger.warning("preview_coverage_gap page=%s reason=%s", event["pagename"], reason)
            if not dry_run:
                runtime._notify_admin(f"preview_coverage_{event['pagename']}",
                    f"Не готов анонс {event['name']} (старт {event['start']}): {reason}. "
                    "Нужна проверка редакционного профиля до окна публикации; "
                    "после начала автоматический анонс не отправляется.")
    result = {"status": "partial" if scan.row_issues else "gaps" if issues else "ok",
              "events_checked": len(events), "issues": issues}
    if scan.row_issues:
        result["row_issues"] = scan.row_issues
    return result
