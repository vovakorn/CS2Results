from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from pydantic import ValidationError

from .. import config as source_config
from ..models import MapResult, MatchNormalized, SourceUnavailableError
from .http_utils import read_limited_response

logger = logging.getLogger(__name__)

LIQUIPEDIA_MATCH_PATH = "/match"
LIQUIPEDIA_MATCH_QUERY = (
    "pagename,match2id,match2bracketid,tournament,tickername,series,date,dateexact,"
    "type,finished,winner,bestof,resulttype,status,section,vod,liquipediatier,"
    "liquipediatiertype,publishertier,match2opponents,match2games"
)

LIQUIPEDIA_TIER_MAP = {
    "1": "s",
    "2": "a",
    "3": "b",
    "4": "c",
    "5": "d",
    "s": "s",
    "a": "a",
    "b": "b",
    "c": "c",
    "d": "d",
}


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _normalize_tier(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold().removesuffix("-tier").strip()
    return LIQUIPEDIA_TIER_MAP.get(normalized)


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_games(value: Any) -> list[MapResult]:
    games = _json_value(value)
    if not isinstance(games, list):
        return []

    maps: list[MapResult] = []
    for game in games[:10]:
        if not isinstance(game, dict):
            continue
        map_name = game.get("map")
        scores = _json_value(game.get("scores"))
        score1 = score2 = None
        if isinstance(scores, list) and len(scores) >= 2:
            score1 = _optional_int(scores[0])
            score2 = _optional_int(scores[1])
        if map_name:
            maps.append(MapResult(name=str(map_name), score1=score1, score2=score2))
    return maps


def _normalize_item(item: dict[str, Any]) -> MatchNormalized | None:
    if str(item.get("finished") or "").strip().casefold() not in {"1", "true", "yes"}:
        return None

    opponents = _json_value(item.get("match2opponents"))
    if not isinstance(opponents, list) or len(opponents) != 2:
        return None
    if not all(isinstance(opponent, dict) for opponent in opponents):
        return None

    team1 = opponents[0].get("name") or opponents[0].get("template")
    team2 = opponents[1].get("name") or opponents[1].get("template")
    score1 = _optional_int(opponents[0].get("score"))
    score2 = _optional_int(opponents[1].get("score"))
    match_id = item.get("match2id")
    tournament = item.get("tournament") or item.get("series") or item.get("tickername")
    if not all((team1, team2, match_id, tournament)):
        return None

    page_name = item.get("pagename")
    match_url = None
    if page_name:
        safe_page = quote(str(page_name).replace(" ", "_"), safe="/:_()-")
        match_url = f"https://liquipedia.net/{source_config.LIQUIPEDIA_WIKI}/{safe_page}"

    event_type = str(item.get("type") or "").strip().casefold()
    is_lan = True if event_type == "offline" else False if event_type == "online" else None
    match_date = item.get("date")
    series_score = max(score1 or 0, score2 or 0)
    best_of = _optional_int(item.get("bestof"))
    if best_of not in {1, 3, 5}:
        best_of = {1: 1, 2: 3, 3: 5}.get(series_score)

    team1_status = _optional_text(opponents[0].get("status"))
    team2_status = _optional_text(opponents[1].get("status"))
    result_type = _optional_text(item.get("resulttype"))
    technical_statuses = {"ff", "dq", "w", "l"}
    forfeit = any(
        value and value.casefold() in technical_statuses
        for value in (team1_status, team2_status)
    ) or (result_type is not None and result_type.casefold() == "default")
    return MatchNormalized(
        source="liquipedia",
        match_id=str(match_id),
        match_url=match_url,
        tournament_name=str(tournament),
        competition_key=str(tournament),
        tournament_tier=_normalize_tier(item.get("liquipediatier")),
        tournament_tier_type=_optional_text(item.get("liquipediatiertype")),
        publisher_tier=_optional_text(item.get("publishertier")),
        tournament_section=_optional_text(item.get("section")),
        team1_name=str(team1),
        team2_name=str(team2),
        score1=score1,
        score2=score2,
        status="finished",
        best_of=best_of,
        maps=_normalize_games(item.get("match2games")),
        date=str(match_date) if match_date else None,
        start_date=str(match_date) if match_date else None,
        end_date=None,
        forfeit=forfeit,
        result_type=result_type,
        team1_result_status=team1_status,
        team2_result_status=team2_status,
        date_exact=_optional_bool(item.get("dateexact")),
        vod_url=_optional_text(item.get("vod")),
        is_lan=is_lan,
        location=None,
        prize_pool_usd=None,
        operator=None,
    )


def _normalize_raw_matches(data: Any) -> list[MatchNormalized]:
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise SourceUnavailableError("Liquipedia returned an unexpected response")

    matches: list[MatchNormalized] = []
    for item in data["result"]:
        if not isinstance(item, dict):
            continue
        try:
            match = _normalize_item(item)
        except (TypeError, ValueError, ValidationError) as exc:
            logger.warning(
                "source=liquipedia item_invalid error_type=%s keys=%s",
                type(exc).__name__,
                sorted(item.keys()),
            )
            continue
        if match:
            matches.append(match)
    return matches


async def fetch_finished_matches(limit: int = 30) -> list[MatchNormalized]:
    api_key = source_config.LIQUIPEDIA_API_KEY
    if not api_key:
        raise SourceUnavailableError("Liquipedia credentials are not configured")

    timeout = aiohttp.ClientTimeout(total=source_config.REQUEST_TIMEOUT_SECONDS)
    headers = {
        "Authorization": f"Apikey {api_key}",
        "Accept": "application/json",
        "User-Agent": "CS2ResultsBot/0.2 (https://github.com/vovakorn/CS2ResultsBot)",
    }
    params = {
        "wiki": source_config.LIQUIPEDIA_WIKI,
        "conditions": "[[finished::1]]",
        "query": LIQUIPEDIA_MATCH_QUERY,
        "order": "date DESC",
        "limit": min(max(limit, 1), 100),
        "offset": 0,
    }
    url = f"{source_config.LIQUIPEDIA_API_BASE_URL.rstrip('/')}{LIQUIPEDIA_MATCH_PATH}"

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params, allow_redirects=False) as response:
                if response.status >= 300:
                    raise SourceUnavailableError(f"Liquipedia returned HTTP {response.status}")
                raw = await read_limited_response(
                    response,
                    source_config.MAX_SOURCE_RESPONSE_BYTES,
                    "Liquipedia",
                )
                data = json.loads(raw)
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"Liquipedia request failed: {type(exc).__name__}") from exc

    matches = _normalize_raw_matches(data)
    logger.info("source=liquipedia normalized=%s", len(matches))
    return matches[:limit]
