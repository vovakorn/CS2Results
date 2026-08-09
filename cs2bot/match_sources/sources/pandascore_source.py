from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

import aiohttp
from pydantic import ValidationError

from .. import config as source_config
from ..filters import is_featured_upcoming
from ..models import (
    MatchNormalized,
    SourceReferences,
    SourceUnavailableError,
    UpcomingMatchNormalized,
)
from .http_utils import read_limited_response

logger = logging.getLogger(__name__)

PANDASCORE_PAST_MATCHES_PATH = "/csgo/matches/past"
PANDASCORE_UPCOMING_MATCHES_PATH = "/csgo/matches/upcoming"
PANDASCORE_TOURNAMENTS_PATH = "/tournaments"
MIN_PANDASCORE_QUERY_WINDOW_HOURS = 24 * 7
MAX_TOURNAMENT_TIER_CACHE_SIZE = 512

TournamentTier = Literal["s", "a", "b", "c", "d"]
_tournament_tier_cache: dict[str, TournamentTier | None] = {}


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_source_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    result = str(value).strip()
    return result or None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        result = value.get("name") or value.get("full_name")
        return str(result).strip() if result else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _image_urls(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    urls: list[str] = []
    for field in ("image_url", "dark_mode_image_url"):
        image_url = value.get(field)
        if isinstance(image_url, str) and image_url.strip():
            cleaned = image_url.strip()
            if cleaned not in urls:
                urls.append(cleaned)
    urls.extend([None, None])
    return urls[0], urls[1]


def _tournament_name(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for value in (item.get("league"), item.get("serie"), item.get("tournament")):
        name = _name(value)
        if name and name.casefold() not in {part.casefold() for part in parts}:
            parts.append(name)
    return " — ".join(parts) if parts else None


def _competition_key(item: dict[str, Any]) -> str | None:
    return _name(item.get("serie")) or _name(item.get("league")) or _name(item.get("tournament"))


def _nested_id(value: Any) -> str | None:
    return _optional_source_id(value.get("id")) if isinstance(value, dict) else None


def _tournament_tier(value: Any) -> TournamentTier | None:
    if not isinstance(value, dict):
        return None
    tier = value.get("tier")
    if not isinstance(tier, str):
        return None
    normalized = tier.strip().casefold()
    if normalized in {"s", "a", "b", "c", "d"}:
        return cast(TournamentTier, normalized)
    return None


def _source_references(
    item: dict[str, Any],
    team1_id: int | None,
    team2_id: int | None,
) -> SourceReferences:
    return SourceReferences(
        league_id=_nested_id(item.get("league")),
        serie_id=_nested_id(item.get("serie")),
        tournament_id=_nested_id(item.get("tournament")),
        team1_id=_optional_source_id(team1_id),
        team2_id=_optional_source_id(team2_id),
        winner_team_id=_optional_source_id(item.get("winner_id")),
    )


def _utc_range(start: datetime, end: datetime) -> str:
    values = []
    for value in (start, end):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        values.append(value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    return ",".join(values)


def _recent_match_range(now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    start = reference - timedelta(
        hours=max(source_config.MAX_SOURCE_STALENESS_HOURS, MIN_PANDASCORE_QUERY_WINDOW_HOURS)
    )
    end = reference + timedelta(hours=source_config.MAX_SOURCE_FUTURE_SKEW_HOURS)
    return _utc_range(start, end)


def _normalize_item(item: dict[str, Any]) -> MatchNormalized | None:
    if str(item.get("status") or "").strip().casefold() != "finished":
        return None

    opponents = item.get("opponents")
    if not isinstance(opponents, list) or len(opponents) != 2:
        return None

    normalized_opponents: list[tuple[str, int | None, str | None, str | None]] = []
    for entry in opponents:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        team_name = _name(opponent)
        team_id = _optional_int(opponent.get("id")) if isinstance(opponent, dict) else None
        if not team_name:
            return None
        logo_url, logo_fallback_url = _image_urls(opponent)
        normalized_opponents.append((team_name, team_id, logo_url, logo_fallback_url))

    scores_by_team: dict[int, int] = {}
    results = item.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            team_id = _optional_int(result.get("team_id"))
            score = _optional_int(result.get("score"))
            if team_id is not None and score is not None:
                scores_by_team[team_id] = score

    score1 = scores_by_team.get(normalized_opponents[0][1]) if normalized_opponents[0][1] is not None else None
    score2 = scores_by_team.get(normalized_opponents[1][1]) if normalized_opponents[1][1] is not None else None
    if (score1 is None or score2 is None) and isinstance(results, list) and len(results) == 2:
        ordered_scores = [_optional_int(result.get("score")) if isinstance(result, dict) else None for result in results]
        if all(score is not None for score in ordered_scores):
            score1, score2 = ordered_scores

    tournament_name = _tournament_name(item)
    match_id = item.get("id")
    if not tournament_name or match_id is None:
        return None

    end_at = item.get("end_at")
    begin_at = item.get("begin_at") or item.get("scheduled_at")
    return MatchNormalized(
        source="pandascore",
        match_id=str(match_id),
        match_url=None,
        tournament_name=tournament_name,
        competition_key=_competition_key(item),
        source_refs=_source_references(
            item,
            normalized_opponents[0][1],
            normalized_opponents[1][1],
        ),
        tournament_tier=_tournament_tier(item.get("tournament")),
        team1_name=normalized_opponents[0][0],
        team2_name=normalized_opponents[1][0],
        team1_logo_url=normalized_opponents[0][2],
        team2_logo_url=normalized_opponents[1][2],
        team1_logo_fallback_url=normalized_opponents[0][3],
        team2_logo_fallback_url=normalized_opponents[1][3],
        score1=score1,
        score2=score2,
        status="finished",
        best_of=_optional_int(item.get("number_of_games")),
        maps=[],
        date=str(end_at or begin_at) if end_at or begin_at else None,
        start_date=str(begin_at) if begin_at else None,
        end_date=str(end_at) if end_at else None,
        original_scheduled_at=_optional_text(item.get("original_scheduled_at")),
        rescheduled=_optional_bool(item.get("rescheduled")),
        forfeit=_optional_bool(item.get("forfeit")),
        is_lan=None,
        location=None,
        prize_pool_usd=None,
        operator=_name(item.get("league")),
    )


def _normalize_raw_matches(data: Any) -> list[MatchNormalized]:
    if not isinstance(data, list):
        raise SourceUnavailableError("PandaScore returned an unexpected response")

    matches: list[MatchNormalized] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            match = _normalize_item(item)
        except (TypeError, ValueError, ValidationError) as exc:
            logger.warning(
                "source=pandascore item_invalid error_type=%s keys=%s",
                type(exc).__name__,
                sorted(item.keys()),
            )
            continue
        if match:
            matches.append(match)
    return matches


def _normalize_upcoming_item(item: dict[str, Any]) -> UpcomingMatchNormalized | None:
    if str(item.get("status") or "").strip().casefold() != "not_started":
        return None
    opponents = item.get("opponents")
    if not isinstance(opponents, list) or len(opponents) != 2:
        return None
    teams: list[tuple[str, int | None, str | None, str | None]] = []
    for entry in opponents:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        team_name = _name(opponent)
        team_id = _optional_int(opponent.get("id")) if isinstance(opponent, dict) else None
        if not team_name:
            return None
        logo_url, logo_fallback_url = _image_urls(opponent)
        teams.append((team_name, team_id, logo_url, logo_fallback_url))
    tournament_name = _tournament_name(item)
    match_id = item.get("id")
    scheduled_at = item.get("scheduled_at") or item.get("begin_at")
    if not tournament_name or match_id is None or not scheduled_at:
        return None
    match = UpcomingMatchNormalized(
        match_id=str(match_id),
        tournament_name=tournament_name,
        competition_key=_competition_key(item),
        source_refs=_source_references(item, teams[0][1], teams[1][1]),
        tournament_tier=_tournament_tier(item.get("tournament")),
        team1_name=teams[0][0],
        team2_name=teams[1][0],
        team1_logo_url=teams[0][2],
        team2_logo_url=teams[1][2],
        team1_logo_fallback_url=teams[0][3],
        team2_logo_fallback_url=teams[1][3],
        scheduled_at=str(scheduled_at),
        original_scheduled_at=_optional_text(item.get("original_scheduled_at")),
        rescheduled=_optional_bool(item.get("rescheduled")),
        forfeit=_optional_bool(item.get("forfeit")),
        best_of=_optional_int(item.get("number_of_games")),
    )
    featured, reason = is_featured_upcoming(match)
    return match.model_copy(update={"is_featured": featured, "feature_reason": reason})


def _normalize_raw_upcoming(data: Any) -> list[UpcomingMatchNormalized]:
    if not isinstance(data, list):
        raise SourceUnavailableError("PandaScore returned an unexpected response")
    matches: list[UpcomingMatchNormalized] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            match = _normalize_upcoming_item(item)
        except (TypeError, ValueError, ValidationError):
            logger.warning("source=pandascore upcoming_item_invalid keys=%s", sorted(item.keys()))
            continue
        if match:
            matches.append(match)
    return matches


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "CS2ResultsBot/0.3 (https://github.com/vovakorn/CS2ResultsBot)",
    }


async def _fetch_json(path: str, params: dict[str, Any]) -> Any:
    token = source_config.PANDASCORE_API_TOKEN
    if not token:
        raise SourceUnavailableError("PandaScore credentials are not configured")
    timeout = aiohttp.ClientTimeout(total=source_config.REQUEST_TIMEOUT_SECONDS)
    url = f"{source_config.PANDASCORE_API_BASE_URL.rstrip('/')}{path}"
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers(token)) as session:
            async with session.get(url, params=params, allow_redirects=False) as response:
                if response.status >= 300:
                    raise SourceUnavailableError(f"PandaScore returned HTTP {response.status}")
                raw = await read_limited_response(
                    response,
                    source_config.MAX_SOURCE_RESPONSE_BYTES,
                    "PandaScore",
                )
                return json.loads(raw)
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"PandaScore request failed: {type(exc).__name__}") from exc


def _cache_tournament_tier(tournament_id: str, tier: TournamentTier | None) -> None:
    if len(_tournament_tier_cache) >= MAX_TOURNAMENT_TIER_CACHE_SIZE:
        _tournament_tier_cache.clear()
    _tournament_tier_cache[tournament_id] = tier


def _log_upcoming_tier_diagnostics(matches: list[UpcomingMatchNormalized]) -> None:
    if not matches:
        return
    counts = {
        (tier, featured): sum(
            1
            for match in matches
            if (match.tournament_tier or "unknown") == tier
            and match.is_featured is featured
        )
        for tier in ("s", "a", "b", "c", "d", "unknown")
        for featured in (True, False)
    }
    logger.info(
        (
            "event=pandascore_upcoming_tier_diagnostics "
            "s_featured=%s s_rejected=%s a_featured=%s a_rejected=%s "
            "b_featured=%s b_rejected=%s c_featured=%s c_rejected=%s "
            "d_featured=%s d_rejected=%s unknown_featured=%s unknown_rejected=%s"
        ),
        counts[("s", True)],
        counts[("s", False)],
        counts[("a", True)],
        counts[("a", False)],
        counts[("b", True)],
        counts[("b", False)],
        counts[("c", True)],
        counts[("c", False)],
        counts[("d", True)],
        counts[("d", False)],
        counts[("unknown", True)],
        counts[("unknown", False)],
    )


async def _enrich_tournament_tiers(
    matches: list[MatchNormalized] | list[UpcomingMatchNormalized],
) -> None:
    """Fill missing tiers in one batched, non-critical tournament request."""
    missing_ids: set[str] = set()
    for match in matches:
        tournament_id = match.source_refs.tournament_id if match.source_refs else None
        if not tournament_id:
            continue
        if match.tournament_tier is not None:
            _cache_tournament_tier(tournament_id, match.tournament_tier)
        elif tournament_id not in _tournament_tier_cache:
            missing_ids.add(tournament_id)

    if missing_ids:
        ordered_ids = sorted(missing_ids)
        try:
            data = await _fetch_json(
                PANDASCORE_TOURNAMENTS_PATH,
                {
                    "filter[id]": ",".join(ordered_ids),
                    "per_page": min(len(ordered_ids), 100),
                },
            )
            if not isinstance(data, list):
                raise SourceUnavailableError("PandaScore returned unexpected tournament data")
            for item in data:
                if not isinstance(item, dict):
                    continue
                tournament_id = _optional_source_id(item.get("id"))
                if tournament_id:
                    _cache_tournament_tier(tournament_id, _tournament_tier(item))
        except SourceUnavailableError as exc:
            logger.warning(
                "event=pandascore_tier_enrichment_failed tournaments=%s error=%s",
                len(ordered_ids),
                exc,
            )

    for match in matches:
        tournament_id = match.source_refs.tournament_id if match.source_refs else None
        if match.tournament_tier is None and tournament_id in _tournament_tier_cache:
            match.tournament_tier = _tournament_tier_cache[tournament_id]


async def fetch_finished_matches(
    limit: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MatchNormalized]:
    params = {
        "filter[status]": "finished",
        # PandaScore places undated records before fresh ones even for
        # ``sort=-end_at``. An explicit begin_at range prevents an old/null
        # page from hiding recent completed matches.
        "range[begin_at]": _utc_range(start, end) if start and end else _recent_match_range(),
        "sort": "-begin_at",
        "per_page": min(max(limit, 1), 100),
    }
    data = await _fetch_json(PANDASCORE_PAST_MATCHES_PATH, params)

    matches = _normalize_raw_matches(data)
    await _enrich_tournament_tiers(matches)
    logger.info("source=pandascore normalized=%s", len(matches))
    return matches[:limit]


async def fetch_upcoming_matches(
    start: datetime,
    end: datetime,
    limit: int = 100,
) -> list[UpcomingMatchNormalized]:
    params = {
        "range[begin_at]": _utc_range(start, end),
        "sort": "begin_at",
        "per_page": min(max(limit, 1), 100),
    }
    data = await _fetch_json(PANDASCORE_UPCOMING_MATCHES_PATH, params)
    matches = _normalize_raw_upcoming(data)
    await _enrich_tournament_tiers(matches)
    _log_upcoming_tier_diagnostics(matches)
    logger.info("source=pandascore upcoming_normalized=%s", len(matches))
    return matches[:limit]
