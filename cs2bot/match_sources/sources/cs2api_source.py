from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable

import aiohttp

from ..config import DEFAULT_USER_AGENT, REQUEST_TIMEOUT_SECONDS
from ..models import MatchNormalized, SourceUnavailableError

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


def _normalize_item(item: dict) -> MatchNormalized | None:
    team1 = _team_name(_first_present(item, [("team1",), ("team_a",), ("opponents", "0")]))
    team2 = _team_name(_first_present(item, [("team2",), ("team_b",), ("opponents", "1")]))

    if not team1 and isinstance(item.get("opponents"), list) and len(item["opponents"]) >= 2:
        team1 = _team_name(item["opponents"][0].get("team") if isinstance(item["opponents"][0], dict) else item["opponents"][0])
        team2 = _team_name(item["opponents"][1].get("team") if isinstance(item["opponents"][1], dict) else item["opponents"][1])

    score1 = _first_present(item, [("score1",), ("team1_score",), ("score", "team1"), ("result", "score1")])
    score2 = _first_present(item, [("score2",), ("team2_score",), ("score", "team2"), ("result", "score2")])
    try:
        score1 = int(score1) if score1 is not None else None
        score2 = int(score2) if score2 is not None else None
    except (TypeError, ValueError):
        score1 = score2 = None

    tournament = _first_present(item, [("tournament_name",), ("event", "name"), ("tournament", "name"), ("league", "name")])
    raw_id = _first_present(item, [("id",), ("match_id",), ("slug",)])
    match_id = str(raw_id) if raw_id else None
    match_url = _first_present(item, [("url",), ("match_url",)])

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
        maps=[],
        date=_first_present(item, [("date",), ("finished_at",), ("start_time",)]),
        is_lan=_first_present(item, [("is_lan",), ("event", "is_lan")]),
        location=_first_present(item, [("location",), ("event", "location")]),
        prize_pool_usd=_first_present(item, [("prize_pool_usd",), ("event", "prize_pool_usd")]),
        operator=_first_present(item, [("operator",), ("event", "operator")]),
    )


async def _call_maybe_async(func, *args, **kwargs):
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_raw_matches(data) -> list[MatchNormalized]:
    if isinstance(data, dict):
        raw_matches = data.get("matches") or data.get("results") or data.get("data") or []
    else:
        raw_matches = data

    if not isinstance(raw_matches, Iterable):
        logger.warning("source=cs2api unexpected_response_type=%s", type(raw_matches).__name__)
        return []

    matches: list[MatchNormalized] = []
    for item in raw_matches:
        if not isinstance(item, dict):
            logger.debug("Skipping cs2api non-dict item type=%s", type(item).__name__)
            continue
        normalized = _normalize_item(item)
        if normalized:
            matches.append(normalized)
    return matches


async def _fetch_via_cs2api_library(limit: int = 30) -> list[MatchNormalized]:
    try:
        import cs2api  # type: ignore
    except Exception as exc:
        raise SourceUnavailableError(f"cs2api import failed: {exc}") from exc

    try:
        client = getattr(cs2api, "Client", None) or getattr(cs2api, "CS2", None)
        api = None
        if client is not None:
            api = client()
            fetcher = (
                getattr(api, "finished", None)
                or getattr(api, "finished_matches", None)
                or getattr(api, "get_finished_matches", None)
                or getattr(api, "matches", None)
            )
        else:
            fetcher = (
                getattr(cs2api, "finished", None)
                or getattr(cs2api, "finished_matches", None)
                or getattr(cs2api, "get_finished_matches", None)
                or getattr(cs2api, "matches", None)
            )
        if fetcher is None:
            raise SourceUnavailableError("cs2api has no known finished match method")

        try:
            data = await _call_maybe_async(fetcher, limit=limit)
        except TypeError:
            data = await _call_maybe_async(fetcher)
        finally:
            close = getattr(api, "close", None) if api is not None else None
            if close is not None:
                await _call_maybe_async(close)
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"cs2api request failed: {exc}") from exc

    return _normalize_raw_matches(data)


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
            async with session.get(BO3_MATCHES_URL, params=BO3_FINISHED_MATCHES_PARAMS) as response:
                if response.status >= 400:
                    raise SourceUnavailableError(f"BO3.gg returned HTTP {response.status}")
                data = await response.json()
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"BO3.gg request failed: {exc}") from exc

    return _normalize_raw_matches(data)


async def fetch_finished_matches(limit: int = 30) -> list[MatchNormalized]:
    errors: list[str] = []
    for fetcher_name, fetcher in (
        ("cs2api_library", lambda: _fetch_via_cs2api_library(limit=limit)),
        ("bo3_http", _fetch_via_bo3_http),
    ):
        try:
            matches = await fetcher()
            logger.info("source=cs2api adapter=%s normalized=%s", fetcher_name, len(matches))
            if matches:
                return matches[:limit]
        except SourceUnavailableError as exc:
            errors.append(f"{fetcher_name}: {exc}")
            logger.warning("source=cs2api adapter=%s status=unavailable error=%s", fetcher_name, exc)

    if errors:
        raise SourceUnavailableError("; ".join(errors))
    return []
