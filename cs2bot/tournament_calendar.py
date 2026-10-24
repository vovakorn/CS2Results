"""Announced events and early preparation checks, independent of API coverage."""
from __future__ import annotations

import calendar
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .tournament_preview import PreviewUnavailable, check_profile

logger = logging.getLogger(__name__)
CALENDAR_PATH = Path(os.getenv("TOURNAMENT_PREVIEW_CALENDAR_PATH",
    str(Path(__file__).resolve().parents[1] / "data/tournament_preview_calendar.json")))


class CalendarDates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    source_url: HttpUrl

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("calendar end precedes start")
        return self


class AnnouncedTournament(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    name: str = Field(min_length=3, max_length=200)
    organizer: str = Field(min_length=2, max_length=100)
    start: date | None = None
    end: date | None = None
    month: str | None = Field(default=None, pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    date_status: Literal["confirmed", "calendar_only", "conflict", "month_only"]
    alternate_dates: list[CalendarDates] = Field(default_factory=list, max_length=4)
    prize_pool_usd: int | None = Field(default=None, gt=0, strict=True)
    participant_count: int | None = Field(default=None, gt=0, le=128, strict=True)
    location: str | None = Field(default=None, min_length=2, max_length=200)
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=6)
    checked_on: date
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_dates(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("calendar dates must be provided together")
        if self.start is None:
            if self.date_status != "month_only" or not self.month:
                raise ValueError("unknown exact dates need an explicit month")
        elif self.end < self.start or self.month is not None or self.date_status == "month_only":
            raise ValueError("invalid exact calendar range")
        if self.date_status == "conflict" and not self.alternate_dates:
            raise ValueError("date conflicts need the alternative and its source")
        if self.date_status != "conflict" and self.alternate_dates:
            raise ValueError("alternative dates require conflict status")
        return self


def load_calendar(path=CALENDAR_PATH) -> list[AnnouncedTournament]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) > 200:
        raise ValueError("calendar must be a bounded list")
    entries = [AnnouncedTournament.model_validate(row) for row in data]
    if len({entry.key for entry in entries}) != len(entries):
        raise ValueError("duplicate announced event key")
    return entries


def audit_calendar_plan(profiles, now, runtime, *, dry_run):
    """Warn from D-14; a planned entry never grants publication approval."""
    today = now.astimezone(ZoneInfo("Europe/Moscow")).date()
    try:
        entries = load_calendar(getattr(runtime, "TOURNAMENT_PREVIEW_CALENDAR_PATH", CALENDAR_PATH))
    except (ValueError, OSError):
        logger.warning("preview_calendar_plan_invalid")
        if not dry_run:
            runtime._notify_admin("preview_calendar_plan_invalid",
                "Не прочитан план предтурнирных анонсов. Проверьте файл календаря.")
        return {"status": "unavailable", "entries": [], "issues": []}
    by_key = {profile.key: profile for profile in profiles}
    results, issues = [], []
    for entry in entries:
        profile = by_key.get(entry.key)
        if entry.start is None:
            year, month = map(int, entry.month.split("-"))
            reference = date(year, month, 1)
            last = date(year, month, calendar.monthrange(year, month)[1])
            reason = "exact_dates_missing"
            due = reference - timedelta(days=14) <= today <= last
        else:
            due = 1 <= (entry.start - today).days <= 14
            reason = "dates_conflict" if entry.date_status == "conflict" else None
            if entry.start <= today:
                reason = "started"
            elif profile is None and reason is None:
                reason = "profile_missing"
            elif profile is not None and reason is None:
                try:
                    check_profile(profile, now)
                except PreviewUnavailable as exc:
                    reason = str(exc)
                if reason is None and (
                        min(stage.start for stage in profile.stages) != entry.start
                        or max(stage.end for stage in profile.stages) != entry.end):
                    reason = "profile_dates_conflict"
                if reason is None and (
                        entry.prize_pool_usd is not None
                        and profile.expected_prize_pool_usd is not None
                        and entry.prize_pool_usd != profile.expected_prize_pool_usd
                        or entry.participant_count is not None
                        and profile.expected_participant_count is not None
                        and entry.participant_count != profile.expected_participant_count):
                    reason = "profile_facts_conflict"
        result = {"key": entry.key, "name": entry.name,
                  "start": entry.start.isoformat() if entry.start else None,
                  "end": entry.end.isoformat() if entry.end else None, "month": entry.month,
                  "status": reason or "ready", "date_status": entry.date_status}
        results.append(result)
        if due and reason:
            issues.append(result)
            logger.warning("preview_calendar_not_ready key=%s reason=%s", entry.key, reason)
            if not dry_run:
                runtime._notify_admin(f"preview_calendar_prepare_{entry.key}",
                    f"Нужна подготовка анонса {entry.name}: {reason}. "
                    "До окна публикации проверьте профиль и подтверждённые условия.")
    return {"status": "gaps" if issues else "ok", "entries": results, "issues": issues}
