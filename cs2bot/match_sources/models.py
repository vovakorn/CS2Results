from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import TEAM_ALIASES


class SourceUnavailableError(Exception):
    """Raised when a source cannot be fetched or parsed reliably."""


class MapResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    score1: int | None = Field(default=None, ge=0, le=100)
    score2: int | None = Field(default=None, ge=0, le=100)


class MatchDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    maps: list[MapResult] = Field(default_factory=list)
    location: str | None = None
    prize_pool_usd: int | None = None
    operator: str | None = None
    is_lan: bool | None = None


class SourceReferences(BaseModel):
    """Provider-scoped IDs used for joins, never for cross-source identity."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    league_id: str | None = Field(default=None, max_length=200)
    serie_id: str | None = Field(default=None, max_length=200)
    tournament_id: str | None = Field(default=None, max_length=200)
    team1_id: str | None = Field(default=None, max_length=200)
    team2_id: str | None = Field(default=None, max_length=200)
    winner_team_id: str | None = Field(default=None, max_length=200)


class MatchNormalized(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)

    source: Literal["pandascore", "liquipedia", "cs2api", "hltv"]
    match_id: str | None = Field(default=None, max_length=200)
    match_url: str | None = Field(default=None, max_length=2048)

    tournament_name: str = Field(min_length=1, max_length=300)
    competition_key: str | None = Field(default=None, max_length=300)
    source_refs: SourceReferences | None = None
    tournament_tier: Literal["s", "a", "b", "c", "d"] | None = None
    team1_name: str = Field(min_length=1, max_length=200)
    team2_name: str = Field(min_length=1, max_length=200)
    team1_logo_url: str | None = Field(default=None, max_length=2048)
    team2_logo_url: str | None = Field(default=None, max_length=2048)
    team1_logo_fallback_url: str | None = Field(default=None, max_length=2048)
    team2_logo_fallback_url: str | None = Field(default=None, max_length=2048)

    score1: int | None = Field(ge=0, le=100)
    score2: int | None = Field(ge=0, le=100)
    status: Literal["finished"] = "finished"
    best_of: Literal[1, 3, 5] | None = None

    maps: list[MapResult] = Field(default_factory=list, max_length=10)

    date: str | None = Field(default=None, max_length=100)
    start_date: str | None = Field(default=None, max_length=100)
    end_date: str | None = Field(default=None, max_length=100)
    original_scheduled_at: str | None = Field(default=None, max_length=100)
    rescheduled: bool | None = None
    forfeit: bool | None = None
    is_lan: bool | None = None
    location: str | None = Field(default=None, max_length=300)
    prize_pool_usd: int | None = Field(default=None, ge=0, le=10_000_000_000)
    operator: str | None = Field(default=None, max_length=200)

    is_tier1_lan: bool = False
    filter_reason: str | None = Field(default=None, max_length=200)

    @staticmethod
    def _identity_part(value: str | None) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        normalized = re.sub(r"\W+", " ", normalized, flags=re.UNICODE).strip()
        return TEAM_ALIASES.get(normalized, normalized)

    @property
    def legacy_match_uid(self) -> str:
        """Return the source-specific UID used by deployments before canonical IDs."""
        if self.match_id:
            return f"{self.source}_{self.match_id}"
        if self.match_url:
            safe_url = self.match_url.rstrip("/").split("/")[-1]
            return f"{self.source}_{safe_url}"
        raise ValueError("match_id or match_url is required")

    @property
    def canonical_match_uid(self) -> str:
        """Return a stable cross-source fingerprint when the source data permits it."""
        match_datetime = self.end_date or self.date or self.start_date
        match_day = match_datetime[:10] if match_datetime and len(match_datetime) >= 10 else ""
        if not match_day:
            return self.legacy_match_uid

        team_scores = sorted(
            (
                (self._identity_part(self.team1_name), self.score1),
                (self._identity_part(self.team2_name), self.score2),
            )
        )
        identity = "|".join(
            [
                match_day,
                self._identity_part(self.competition_key or self.tournament_name),
                *(f"{team}:{score if score is not None else ''}" for team, score in team_scores),
            ]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"match_v1_{digest}"

    @property
    def match_uid(self) -> str:
        return self.canonical_match_uid


class UpcomingMatchNormalized(BaseModel):
    """A not-yet-started match used in the daily schedule."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: Literal["pandascore"] = "pandascore"
    match_id: str = Field(min_length=1, max_length=200)
    tournament_name: str = Field(min_length=1, max_length=300)
    competition_key: str | None = Field(default=None, max_length=300)
    source_refs: SourceReferences | None = None
    tournament_tier: Literal["s", "a", "b", "c", "d"] | None = None
    team1_name: str = Field(min_length=1, max_length=200)
    team2_name: str = Field(min_length=1, max_length=200)
    team1_logo_url: str | None = Field(default=None, max_length=2048)
    team2_logo_url: str | None = Field(default=None, max_length=2048)
    team1_logo_fallback_url: str | None = Field(default=None, max_length=2048)
    team2_logo_fallback_url: str | None = Field(default=None, max_length=2048)
    scheduled_at: str = Field(min_length=1, max_length=100)
    original_scheduled_at: str | None = Field(default=None, max_length=100)
    rescheduled: bool | None = None
    forfeit: bool | None = None
    best_of: Literal[1, 3, 5] | None = None
    is_featured: bool = False
    feature_reason: str | None = Field(default=None, max_length=200)
