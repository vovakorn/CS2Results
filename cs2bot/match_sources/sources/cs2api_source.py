from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable

import aiohttp
from pydantic import ValidationError

from ..config import DEFAULT_USER_AGENT, MAX_SOURCE_RESPONSE_BYTES, REQUEST_TIMEOUT_SECONDS
from ..models import MapResult, MatchNormalized, SourceUnavailableError
from .http_utils import read_limited_response

logger = logging.getLogger(__name__)

BO3_MATCHES_URL = "https://api.bo3.gg/api/v1/matches"
BO3_FINISHED_MATCHES_PARAMS = {
    "scope": "widget-matches",
    "page[offset]": 0,
    "page[limit]": 100,
    "sort": "tier_rank,-start_date",
    "filter[matches.status][in]": "finished",
    "filter[matches.discipline_id][eq]": 1,
    "with": "teams,tournament,ai_predictions,games,streams",
}


def _dig(data: dict, *keys: str):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_present(data: dict, paths: list[tuple[str, ...]]):
    for path in paths:
        value = _dig(data, *path)
        if value not in (None, ""):
            return value
    return None


def _team_name(team) -> str | None:
    if isinstance(team, str):
        return team
    if isinstance(team, dict):
        return team.get("name") or team.get("title")
    return None


def _map_name(raw_name: str | None) -> str:
    if not raw_name:
        return "Unknown"
    known = {
        "de_ancient": "Ancient",
        "de_anubis": "Anubis",
        "de_cache": "Cache",
        "de_dust2": "Dust2",
        "de_inferno": "Inferno",
        "de_mirage": "Mirage",
        "de_nuke": "Nuke",
        "de_overpass": "Overpass",
        "de_train": "Train",
        "de_vertigo": "Vertigo",
    }
    if raw_name in known:
        return known[raw_name]
    return raw_name.removeprefix("de_").replace("_", " ").title()


def _team_id(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d[\d,.\s]*", str(value))
    if not match:
        return None
    digits = re.sub(r"[^\d-]", "", match.group(0))
    try:
        return int(digits)
    except ValueError:
        return None


def _optional_str(value) -> str | None:
    return str(value) if value not in (None, "") else None


def _normalize_games(item: dict) -> list[MapResult]:
    games = item.get("games")
    if not isinstance(games, list):
        return []

    team1_id = _team_id(item.get("team1_id") or _dig(item, "team1", "id"))
    team2_id = _team_id(item.get("team2_id") or _dig(item, "team2", "id"))
    maps: list[MapResult] = []

    for game in sorted((game for game in games if isinstance(game, dict)), key=lambda g: g.get("number") or 0):
        winner_id = _team_id(_dig(game, "winner_team_clan", "team_id"))
        loser_id = _team_id(_dig(game, "loser_team_clan", "team_id"))
        winner_score = _first_present(game, [("winner_clan_score",), ("winner_score",), ("score1",)])
        loser_score = _first_present(game, [("loser_clan_score",), ("loser_score",), ("score2",)])
        winner_score = _optional_int(winner_score)
        loser_score = _optional_int(loser_score)

        score1 = score2 = None
        if winner_id is not None and loser_id is not None and team1_id is not None and team2_id is not None:
            if winner_id == team1_id and loser_id == team2_id:
                score1, score2 = winner_score, loser_score
            elif winner_id == team2_id and loser_id == team1_id:
                score1, score2 = loser_score, winner_score

        maps.append(
            MapResult(
                name=_map_name(game.get("map_name") or game.get("name")),
                score1=score1,
                score2=score2,
            )
        )

    return maps


def _normalize_item(item: dict) -> MatchNormalized | None:
    team1 = _team_name(_first_present(item, [("team1",), ("team_a",), ("opponents", "0")]))
    team2 = _team_name(_first_present(item, [("team2",), ("team_b",), ("opponents", "1")]))

    if not team1 and isinstance(item.get("opponents"), list) and len(item["opponents"]) >= 2:
        team1 = _team_name(item["opponents"][0].get("team") if isinstance(item["opponents"][0], dict) else item["opponents"][0])
        team2 = _team_name(item["opponents"][1].get("team") if isinstance(item["opponents"][1], dict) else item["opponents"][1])

    score1 = _first_present(item, [("score1",), ("team1_score",), ("score", "team1"), ("result", "score1")])
    score2 = _first_present(item, [("score2",), ("team2_score",), ("score", "team2"), ("result", "score2")])
    score1 = _optional_int(score1)
    score2 = _optional_int(score2)

    tournament = _first_present(item, [("tournament_name",), ("event", "name"), ("tournament", "name"), ("league", "name")])
    raw_id = _first_present(item, [("id",), ("match_id",), ("slug",)])
    match_id = str(raw_id) if raw_id else None
    match_url = _first_present(item, [("url",), ("match_url",)])
    start_date = _first_present(item, [("start_date",), ("start_time",), ("date",)])
    end_date = _first_present(item, [("end_date",), ("finished_at",)])
    display_date = end_date or start_date

    if not tournament or not team1 or not team2:
        logger.debug("Skipping cs2api item with incomplete structure keys=%s", sorted(item.keys()))
        return None

    return MatchNormalized(
        source="cs2api",
        match_id=match_id,
        match_url=match_url,
        tournament_name=str(tournament),
        team1_name=str(team1),
        team2_name=str(team2),
        score1=score1,
        score2=score2,
        maps=_normalize_games(item),
        date=_optional_str(display_date),
        start_date=_optional_str(start_date),
        end_date=_optional_str(end_date),
        is_lan=_first_present(item, [("is_lan",), ("event", "is_lan")]),
        location=_first_present(item, [("location",), ("event", "location")]),
        prize_pool_usd=_optional_int(
            _first_present(
                item,
                [
                    ("prize_pool_usd",),
                    ("event", "prize_pool_usd"),
                    ("tournament", "prize_pool_usd"),
                    ("tournament", "prize"),
                ],
            )
        ),
        operator=_first_present(item, [("operator",), ("event", "operator")]),
    )


def _normalize_raw_matches(data) -> list[MatchNormalized]:
    if isinstance(data, dict):
        raw_matches = data.get("matches") or data.get("results") or data.get("data") or []
    else:
        raw_matches = data

    if not isinstance(raw_matches, Iterable) or isinstance(raw_matches, (str, bytes)):
        logger.warning("source=cs2api unexpected_response_type=%s", type(raw_matches).__name__)
        return []

    matches: list[MatchNormalized] = []
    for item in raw_matches:
        if not isinstance(item, dict):
            logger.debug("Skipping cs2api non-dict item type=%s", type(item).__name__)
            continue
        try:
            normalized = _normalize_item(item)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "source=cs2api item_invalid error_type=%s keys=%s",
                type(exc).__name__,
                sorted(item.keys()),
            )
            continue
        if normalized:
            matches.append(normalized)
    return matches


async def _fetch_via_bo3_http() -> list[MatchNormalized]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://bo3.gg",
        "Referer": "https://bo3.gg/",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(
                BO3_MATCHES_URL,
                params=BO3_FINISHED_MATCHES_PARAMS,
                allow_redirects=False,
            ) as response:
                if response.status >= 300:
                    raise SourceUnavailableError(f"BO3.gg returned HTTP {response.status}")
                raw = await read_limited_response(response, MAX_SOURCE_RESPONSE_BYTES, "BO3.gg")
                data = json.loads(raw)
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"BO3.gg request failed: {exc}") from exc

    return _normalize_raw_matches(data)


async def fetch_finished_matches(limit: int = 30) -> list[MatchNormalized]:
    matches = await _fetch_via_bo3_http()
    logger.info("source=cs2api adapter=bo3_http normalized=%s", len(matches))
    return matches[:limit]
