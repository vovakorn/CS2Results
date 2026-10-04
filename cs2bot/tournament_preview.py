"""Source-backed tournament passports and reviewed editorial supplements."""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class PreviewUnavailable(ValueError):
    """A preview is incomplete, stale or not eligible for publication."""


class PreviewStage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["groups", "swiss", "playoffs"]
    start: date
    end: date
    format_text: str = Field(min_length=10, max_length=220)
    source_url: HttpUrl
    card_summary: str | None = Field(default=None, min_length=3, max_length=60)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end < self.start:
            raise ValueError("stage end precedes start")
        return self

    @property
    def label(self) -> str:
        return {"groups": "Группы", "swiss": "Swiss", "playoffs": "Плей-офф"}[self.kind]


class PreviewBranding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    accent: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    secondary: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    background: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    motif: Literal["lines", "grid", "diagonal", "ember", "metal", "target",
                   "electric-pulse", "broken-planes", "zigzag", "star", "medal"] | None = None
    theme_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    series_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    organizer_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    theme_source_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)
    logo_asset: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,99}\.png$")
    logo_source_url: HttpUrl | None = None
    headline: str | None = Field(default=None, min_length=5, max_length=80)
    headline_source_url: HttpUrl | None = None

    @model_validator(mode="after")
    def sourced_visuals(self):
        if bool(self.logo_asset) != bool(self.logo_source_url):
            raise ValueError("event logo requires its source URL")
        if bool(self.headline) != bool(self.headline_source_url):
            raise ValueError("headline requires its source URL")
        return self


class PreviewProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$")
    liquipedia_page: str = Field(min_length=1, max_length=300)
    pandascore_serie_id: int | None = Field(default=None, gt=0, strict=True)
    pandascore_tournament_ids: list[int] = Field(default_factory=list, max_length=32)
    approved: bool = False
    verified_at: datetime
    story: str | None = Field(default=None, min_length=20, max_length=240)
    short_story: str | None = Field(default=None, min_length=20, max_length=100)
    story_source_url: HttpUrl | None = None
    stages: list[PreviewStage] = Field(min_length=2, max_length=3)
    prize_money_usd: int | None = Field(default=None, gt=0, strict=True)
    club_reward_usd: int | None = Field(default=None, ge=0, strict=True)
    prize_split_kind: Literal["players_clubs", "prize_participation"] = "players_clubs"
    prize_source_url: HttpUrl | None = None
    venue_scope: Literal["whole_event", "playoffs"] = "whole_event"
    branding: PreviewBranding = Field(default_factory=PreviewBranding)

    @field_validator("liquipedia_page")
    @classmethod
    def safe_page(cls, value):
        if any(char in value for char in "[]{}|\\\n\r"):
            raise ValueError("invalid Liquipedia page")
        return value

    @model_validator(mode="after")
    def reviewed_sources(self):
        if self.pandascore_tournament_ids and not self.pandascore_serie_id:
            raise ValueError("stage IDs require the explicit PandaScore serie")
        if len(set(self.pandascore_tournament_ids)) != len(self.pandascore_tournament_ids):
            raise ValueError("duplicate PandaScore stage IDs")
        if self.verified_at.tzinfo is None:
            raise ValueError("verified_at needs a timezone")
        if bool(self.story) != bool(self.story_source_url):
            raise ValueError("story requires its source URL")
        if self.short_story and not self.story:
            raise ValueError("short story requires the reviewed full story and its source")
        if (self.prize_money_usd is None) != (self.club_reward_usd is None):
            raise ValueError("prize split requires both amounts")
        if self.prize_money_usd is not None and self.prize_source_url is None:
            raise ValueError("prize split requires its source URL")
        kinds = [stage.kind for stage in self.stages]
        if len(kinds) != len(set(kinds)) or "playoffs" not in kinds or not {"groups", "swiss"}.intersection(kinds):
            raise ValueError("stages need unique kinds, an initial stage and playoffs")
        initial = [stage for stage in self.stages if stage.kind != "playoffs"]
        playoffs = next(stage for stage in self.stages if stage.kind == "playoffs")
        if max(stage.end for stage in initial) > playoffs.start:
            raise ValueError("playoffs must follow the initial stage")
        self.stages.sort(key=lambda stage: stage.start)
        from .tournament_branding import resolve_branding
        self.branding = resolve_branding(self.branding, event_key=self.key)
        return self

    @field_validator("pandascore_tournament_ids", mode="before")
    @classmethod
    def strict_stage_ids(cls, value):
        if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("stage IDs must be positive integers")
        return value


class PreviewTeam(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    team_id: str
    name: str = Field(min_length=1, max_length=200)
    logo_url: str | None = None


class TournamentPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str
    name: str = Field(min_length=1, max_length=300)
    start: date
    end: date
    prize_pool_usd: int = Field(gt=0, le=10_000_000_000, strict=True)
    prize_money_usd: int | None = Field(default=None, gt=0, strict=True)
    club_reward_usd: int | None = Field(default=None, ge=0, strict=True)
    prize_split_kind: Literal["players_clubs", "prize_participation"] = "players_clubs"
    prize_source_url: str | None = None
    locations: list[str] = Field(min_length=1, max_length=8)
    participant_count: int | None = Field(default=None, gt=0, le=128, strict=True)
    teams: list[PreviewTeam] = Field(default_factory=list, max_length=128)
    first_match_at: datetime | None = None
    story: str
    short_story: str | None = Field(default=None, min_length=20, max_length=100)
    story_source_url: str
    stages: list[PreviewStage] = Field(min_length=2, max_length=3)
    passport_source_url: str
    participants_source_url: str | None = None
    verified_at: datetime
    fetched_at: datetime
    warnings: list[str] = Field(default_factory=list)
    branding: PreviewBranding = Field(default_factory=PreviewBranding)

    @model_validator(mode="after")
    def valid_passport(self):
        if self.end < self.start:
            raise ValueError("tournament end precedes start")
        if (self.prize_money_usd is None) != (self.club_reward_usd is None):
            raise ValueError("incomplete prize split")
        if self.prize_money_usd is not None and not self.prize_source_url:
            raise ValueError("prize split requires its source URL")
        if self.prize_money_usd is not None and self.prize_money_usd + self.club_reward_usd != self.prize_pool_usd:
            raise ValueError("prize split conflicts with API total")
        if any(stage.start < self.start or stage.end > self.end for stage in self.stages):
            raise ValueError("stage outside tournament dates")
        if min(stage.start for stage in self.stages) != self.start or max(stage.end for stage in self.stages) != self.end:
            raise ValueError("reviewed stages conflict with tournament dates")
        if self.teams and self.participant_count != len(self.teams):
            raise ValueError("partial participants must not be shown as a complete list")
        if self.first_match_at is not None:
            if self.first_match_at.tzinfo is None:
                raise ValueError("first match timestamp needs a timezone")
            local_date = self.first_match_at.astimezone(ZoneInfo("Europe/Moscow")).date()
            if not self.start <= local_date <= self.end:
                raise ValueError("first match outside tournament dates")
        from .tournament_branding import resolve_branding
        self.branding = resolve_branding(self.branding, event_key=self.key)
        return self


def load_profiles(path: str | Path) -> list[PreviewProfile]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("preview profiles must be a JSON list")
    profiles = [PreviewProfile.model_validate(item) for item in data]
    if len({profile.key for profile in profiles}) != len(profiles):
        raise ValueError("duplicate preview profile keys")
    pages, series, stages = set(), set(), set()
    for profile in profiles:
        if profile.liquipedia_page in pages or (profile.pandascore_serie_id is not None and profile.pandascore_serie_id in series):
            raise ValueError("ambiguous tournament profile identity")
        if stages.intersection(profile.pandascore_tournament_ids):
            raise ValueError("ambiguous PandaScore stage identity")
        pages.add(profile.liquipedia_page)
        if profile.pandascore_serie_id is not None:
            series.add(profile.pandascore_serie_id)
        stages.update(profile.pandascore_tournament_ids)
    return profiles


def check_profile(profile: PreviewProfile, now: datetime, *, allow_draft: bool = False) -> None:
    if not profile.approved and not allow_draft:
        raise PreviewUnavailable("profile_not_approved")
    age = now.astimezone(timezone.utc) - profile.verified_at.astimezone(timezone.utc)
    if age < timedelta(0) or age > timedelta(days=7):
        raise PreviewUnavailable("editorial_review_stale")


def publication_window(preview: TournamentPreview, now: datetime) -> bool:
    """Publish once, on one of the two Moscow calendar days before the start."""
    if preview.first_match_at and preview.first_match_at <= now:
        return False
    days = (preview.start - now.astimezone(ZoneInfo("Europe/Moscow")).date()).days
    return days in {1, 2}


def date_range(start: date, end: date, *, year: bool = False) -> str:
    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")
    if start.year != end.year:
        return (f"{start.day} {months[start.month - 1]} {start.year} – "
                f"{end.day} {months[end.month - 1]} {end.year}")
    if start == end:
        text = f"{start.day} {months[start.month - 1]}"
    elif start.month == end.month:
        text = f"{start.day}–{end.day} {months[end.month - 1]}"
    else:
        text = f"{start.day} {months[start.month - 1]} – {end.day} {months[end.month - 1]}"
    return f"{text} {end.year}" if year else text


def liquipedia_url(page: str) -> str:
    return "https://liquipedia.net/counterstrike/" + quote(page, safe="/")


def _source_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def prize_split_text(preview: TournamentPreview, *, separator=" · ") -> str:
    labels = ("Игрокам", "клубам") if preview.prize_split_kind == "players_clubs" else ("Призовые", "за участие")
    return (f"{labels[0]} ${preview.prize_money_usd:,}{separator}"
            f"{labels[1]} ${preview.club_reward_usd:,}").replace(",", " ")


def format_preview_caption(preview: TournamentPreview, *, draft: bool = False) -> str:
    """Keep the complete story and required facts together in one album caption."""
    esc = html.escape
    label = "Черновик · Перед стартом" if draft else "Перед стартом"
    lines = [f"<b>{label} · {esc(preview.name)}</b>", "", esc(preview.story), "",
             (f"Призовые и выплаты: ${preview.prize_pool_usd:,}" if preview.prize_money_usd is not None
              else f"Призовой фонд: ${preview.prize_pool_usd:,}").replace(",", " "),
             f"Даты: {date_range(preview.start, preview.end, year=True)}",
             "📍 " + esc(" / ".join(preview.locations))]
    if preview.prize_money_usd is not None:
        lines.insert(5, prize_split_text(preview, separator="; ") + ".")
    lines.append("Формат:")
    for stage in preview.stages:
        lines.append(f"{stage.label} · {date_range(stage.start, stage.end)}: {esc(stage.format_text)}")
    if preview.first_match_at:
        local = preview.first_match_at.astimezone(ZoneInfo("Europe/Moscow"))
        lines.append(f"Первый матч: {date_range(local.date(), local.date())}, {local:%H:%M} МСК")
    sources = {preview.passport_source_url: "Liquipedia", preview.story_source_url: "История"}
    for stage in preview.stages:
        sources.setdefault(str(stage.source_url), "Регламент")
    if preview.prize_source_url:
        sources.setdefault(preview.prize_source_url, "Призовые")
    lines.extend(["", "Источники: " + " · ".join(_source_link(url, label) for url, label in sources.items())])
    text = "\n".join(lines)
    if len(text) > 1024 or len(re.sub(r"<[^>]+>", "", text).encode("utf-16-le")) // 2 > 1024:
        raise PreviewUnavailable("caption_too_long")
    return text


def format_preview_instagram_caption(preview: TournamentPreview) -> str:
    """Keep the complete caption and source URLs in plain text."""
    text = format_preview_caption(preview)
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r'\2: \1', text)
    text = html.unescape(re.sub(r"</?b>", "", text))
    if len(text) > 2200:
        raise PreviewUnavailable("instagram_caption_too_long")
    return text


def format_preview_threads_caption(preview: TournamentPreview) -> str:
    """Use reviewed short history and full stage facts; never truncate facts."""
    lines = [f"Перед стартом · {preview.name}", preview.short_story or preview.story,
             date_range(preview.start, preview.end), "📍 " + " / ".join(preview.locations),
             f"Призовые: ${preview.prize_pool_usd:,}".replace(",", " ")]
    if preview.prize_money_usd is not None:
        lines.append(prize_split_text(preview))
    lines.append("Формат:")
    for stage in preview.stages:
        lines.append(f"{stage.label} {date_range(stage.start, stage.end)}: {stage.format_text}")
    if preview.first_match_at:
        local = preview.first_match_at.astimezone(ZoneInfo("Europe/Moscow"))
        lines.append(f"Первый матч: {date_range(local.date(), local.date())}, {local:%H:%M} МСК")
    lines.append(preview.passport_source_url)
    text = "\n".join(lines)
    if len(text.encode("utf-16-le")) // 2 > 500:
        raise PreviewUnavailable("threads_caption_too_long")
    return text
