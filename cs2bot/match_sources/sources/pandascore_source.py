from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from pydantic import ValidationError

from .. import config as source_config
from ..filters import is_featured_upcoming
from ..models import MatchNormalized, SourceUnavailableError, UpcomingMatchNormalized
from .http_utils import read_limited_response

logger = logging.getLogger(__name__)

PANDASCORE_PAST_MATCHES_PATH = "/csgo/matches/past"
PANDASCORE_UPCOMING_MATCHES_PATH = "/csgo/matches/upcoming"
MIN_PANDASCORE_QUERY_WINDOW_HOURS = 24 * 7


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        result = value.get("name") or value.get("full_name")
        return str(result).strip() if result else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _tournament_name(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for value in (item.get("league"), item.get("serie"), item.get("tournament")):
        name = _name(value)
        if name and name.casefold() not in {part.casefold() for part in parts}:
            parts.append(name)
    return " — ".join(parts) if parts else None


def _competition_key(item: dict[str, Any]) -> str | None:
    return _name(item.get("serie")) or _name(item.get("league")) or _name(item.get("tournament"))


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

    normalized_opponents: list[tuple[str, int | None]] = []
    for entry in opponents:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        team_name = _name(opponent)
        team_id = _optional_int(opponent.get("id")) if isinstance(opponent, dict) else None
        if not team_name:
            return None
        normalized_opponents.append((team_name, team_id))

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
        team1_name=normalized_opponents[0][0],
        team2_name=normalized_opponents[1][0],
        score1=score1,
        score2=score2,
        status="finished",
        best_of=_optional_int(item.get("number_of_games")),
        maps=[],
        date=str(end_at or begin_at) if end_at or begin_at else None,
        start_date=str(begin_at) if begin_at else None,
        end_date=str(end_at) if end_at else None,
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
    teams: list[str] = []
    for entry in opponents:
        opponent = entry.get("opponent") if isinstance(entry, dict) else None
        team_name = _name(opponent)
        if not team_name:
            return None
        teams.append(team_name)
    tournament_name = _tournament_name(item)
    match_id = item.get("id")
    scheduled_at = item.get("scheduled_at") or item.get("begin_at")
    if not tournament_name or match_id is None or not scheduled_at:
        return None
    match = UpcomingMatchNormalized(
        match_id=str(match_id),
        tournament_name=tournament_name,
        competition_key=_competition_key(item),
        team1_name=teams[0],
        team2_name=teams[1],
        scheduled_at=str(scheduled_at),
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
    logger.info("source=pandascore upcoming_normalized=%s", len(matches))
    return matches[:limit]
