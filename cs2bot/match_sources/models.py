from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceUnavailableError(Exception):
    """Raised when a source cannot be fetched or parsed reliably."""


class MapResult(BaseModel):
    name: str
    score1: int | None = None
    score2: int | None = None


class MatchDetails(BaseModel):
    maps: list[MapResult] = Field(default_factory=list)
    location: str | None = None
    prize_pool_usd: int | None = None
    operator: str | None = None
    is_lan: bool | None = None


class MatchNormalized(BaseModel):
    source: Literal["cs2api", "hltv"]
    match_id: str | None = None
    match_url: str | None = None

    tournament_name: str
    team1_name: str
    team2_name: str

    score1: int | None
    score2: int | None

    maps: list[MapResult] = Field(default_factory=list)

    date: str | None = None
    is_lan: bool | None = None
    location: str | None = None
    prize_pool_usd: int | None = None
    operator: str | None = None

    is_tier1_lan: bool = False
    filter_reason: str | None = None

    @property
    def match_uid(self) -> str:
        if self.match_id:
            return f"{self.source}_{self.match_id}"
        if self.match_url:
            safe_url = self.match_url.rstrip("/").split("/")[-1]
            return f"{self.source}_{safe_url}"
        raise ValueError("match_id or match_url is required")

