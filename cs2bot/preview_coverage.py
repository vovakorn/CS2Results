"""Read-only calendar audit; never creates editorial profiles or event identity."""
from __future__ import annotations

import asyncio
import logging

from .match_sources.sources.tournament_preview_source import fetch_preview_calendar
from .tournament_preview import PreviewUnavailable, check_profile

logger = logging.getLogger(__name__)


def audit_preview_coverage(profiles, now, runtime, *, dry_run):
    """Report missing/stale/different-date profiles without changing publication."""
    try:
        events = asyncio.run(fetch_preview_calendar(now))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PreviewUnavailable) else type(exc).__name__
        logger.warning("preview_coverage_unavailable reason=%s", reason)
        if not dry_run:
            runtime._notify_admin("preview_coverage_unavailable",
                "Не проверено покрытие предтурнирных анонсов: календарь недоступен. "
                "Проверенные профили обрабатываются отдельно.")
        return {"status": "unavailable", "reason": reason, "issues": []}
    by_page = {profile.liquipedia_page: profile for profile in profiles}
    issues = []
    for event in events:
        profile = by_page.get(event["pagename"])
        reason = None
        if profile is None:
            reason = "preview_profile_missing"
        else:
            try:
                check_profile(profile, now)
            except PreviewUnavailable as exc:
                reason = str(exc)
            if reason is None and (
                    min(stage.start for stage in profile.stages).isoformat() != event["start"]
                    or max(stage.end for stage in profile.stages).isoformat() != event["end"]):
                reason = "preview_profile_dates_conflict"
        if reason:
            issues.append({**event, "reason": reason})
            logger.warning("preview_coverage_gap page=%s reason=%s", event["pagename"], reason)
            if not dry_run:
                runtime._notify_admin(f"preview_coverage_{event['pagename']}",
                    f"Не готов анонс {event['name']} (старт {event['start']}): {reason}. "
                    "Нужна проверка редакционного профиля до окна публикации; "
                    "после начала автоматический анонс не отправляется.")
    return {"status": "gaps" if issues else "ok", "events_checked": len(events), "issues": issues}
