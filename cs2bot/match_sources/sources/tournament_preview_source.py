"""Read-only enrichment using the existing Liquipedia and PandaScore credentials."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from .. import config
from . import pandascore_source
from .http_utils import read_limited_response
from .pandascore_context import _earliest_match_at
from ...tournament_preview import (
    PreviewProfile, PreviewTeam, PreviewUnavailable, TournamentPreview,
    check_profile, liquipedia_url,
)

PASSPORT_QUERY = "pagename,name,startdate,enddate,prizepool,locations,participantsnumber,previous"
COUNTRY_NAMES = {"PL": "Польша", "DK": "Дания", "DE": "Германия", "US": "США", "CN": "Китай",
                 "RO": "Румыния", "GB": "Великобритания", "MT": "Мальта", "SA": "Саудовская Аравия",
                 "SG": "Сингапур", "FR": "Франция", "BR": "Бразилия", "RS": "Сербия",
                 "SE": "Швеция", "TR": "Турция", "AE": "ОАЭ", "PT": "Португалия", "ES": "Испания",
                 "HK": "Гонконг"}
CITY_NAMES = {"Katowice": "Катовице", "Copenhagen": "Копенгаген", "Bucharest": "Бухарест",
              "Stockholm": "Стокгольм", "Cologne": "Кёльн", "Paris": "Париж", "Porto": "Порту",
              "Chek Lap Kok": "Чхек-Лап-Кок"}


async def _liquipedia_json(path: str, params: dict[str, Any]) -> Any:
    if not config.LIQUIPEDIA_API_KEY:
        raise PreviewUnavailable("liquipedia_credentials_missing")
    headers = {"Authorization": f"Apikey {config.LIQUIPEDIA_API_KEY}",
               "Accept": "application/json", "User-Agent": "CS2ResultsBot/0.2"}
    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(config.LIQUIPEDIA_API_BASE_URL.rstrip("/") + path,
                               params={"wiki": config.LIQUIPEDIA_WIKI, **params},
                               allow_redirects=False) as response:
            if response.status != 200:
                raise PreviewUnavailable(f"liquipedia_http_{response.status}")
            raw = await read_limited_response(response, config.MAX_SOURCE_RESPONSE_BYTES, "preview")
            return json.loads(raw)


def _result(data: Any) -> list[dict]:
    items = data.get("result") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise PreviewUnavailable("invalid_liquipedia_response")
    return [item for item in items if isinstance(item, dict)]


def _date(value: Any) -> date:
    if not isinstance(value, str):
        raise PreviewUnavailable("tournament_dates_missing")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise PreviewUnavailable("tournament_dates_invalid") from exc


def _integer(value: Any, reason: str, maximum: int = 10_000_000_000) -> int:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value() or not 0 < number <= maximum:
            raise ValueError
        return int(number)
    except (ValueError, InvalidOperation) as exc:
        raise PreviewUnavailable(reason) from exc


def _locations(value: Any, venue_scope="whole_event") -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise PreviewUnavailable("locations_missing")
    locations = []
    for index in range(1, 9):
        country, city, venue = (value.get(f"{field}{index}") for field in ("country", "city", "venue"))
        if not country and not city and not venue:
            continue
        if not isinstance(country, str) or not country.strip() or not isinstance(city, str) or not city.strip():
            raise PreviewUnavailable("location_incomplete")
        code = country.strip().upper()
        parts = [CITY_NAMES.get(city.strip(), city.strip()), COUNTRY_NAMES.get(code, code)]
        if isinstance(venue, str) and venue.strip():
            parts.append(venue.strip() + (" (плей-офф)" if venue_scope == "playoffs" else ""))
        locations.append(", ".join(parts))
    if not locations:
        raise PreviewUnavailable("locations_missing")
    return list(dict.fromkeys(locations))


async def _panda_pages(path: str) -> list[dict]:
    items = []
    for page in range(1, 11):
        data = await pandascore_source._fetch_json(path, {"per_page": 100, "page": page})
        if not isinstance(data, list):
            raise PreviewUnavailable("invalid_pandascore_response")
        items.extend(item for item in data if isinstance(item, dict))
        if len(data) < 100:
            return items
    raise PreviewUnavailable("participants_pagination_incomplete")


async def _participants(serie_id: int) -> tuple[list[PreviewTeam], datetime | None]:
    tournaments = await _panda_pages(f"/series/{serie_id}/tournaments")
    ids = sorted({str(item["id"]) for item in tournaments
                  if item.get("id") is not None and str(item["id"]).isdigit()})
    if not ids or len(ids) > 32:
        raise PreviewUnavailable("stages_unavailable")
    # Tournament IDs belong to stages; collect all of them under the explicit serie join.
    semaphore = asyncio.Semaphore(4)
    async def roster(tournament_id):
        async with semaphore:
            return await pandascore_source._fetch_json(f"/tournaments/{tournament_id}/rosters", {})
    rosters = await asyncio.gather(*(roster(tournament_id) for tournament_id in ids))
    teams: dict[str, PreviewTeam] = {}
    for payload in rosters:
        if isinstance(payload, dict):
            if payload.get("type") not in (None, "Team", "team"):
                raise PreviewUnavailable("participant_type_invalid")
            rows = payload.get("rosters", [])
        else:
            rows = payload
        if not isinstance(rows, list):
            raise PreviewUnavailable("invalid_roster_response")
        # The live API returns Team objects directly under {type, rosters};
        # legacy responses may wrap each object in `team`. Never walk players.
        for item in rows:
            if not isinstance(item, dict):
                continue
            team = item.get("team", item)
            if not isinstance(team, dict) or team.get("id") is None or not team.get("name"):
                continue
            normalized = PreviewTeam(team_id=str(team["id"]), name=team["name"], logo_url=team.get("image_url"))
            old = teams.get(normalized.team_id)
            if old is not None and old.name != normalized.name:
                raise PreviewUnavailable("participant_identity_conflict")
            teams[normalized.team_id] = normalized
    data = await pandascore_source._fetch_json(f"/series/{serie_id}/matches", {"sort": "begin_at", "per_page": 1})
    raw = _earliest_match_at(data)
    first = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    if first and first.tzinfo is None:
        first = None
    return sorted(teams.values(), key=lambda team: (team.name.casefold(), team.team_id)), first


async def _previous_champion(page: str) -> str | None:
    data = await _liquipedia_json("/placement", {
        "conditions": f"[[parent::{page}]] AND [[placement::1]]",
        "query": "placement,opponentname", "limit": 100, "offset": 0,
    })
    rows = _result(data)
    names = {row["opponentname"].strip() for row in rows
             if str(row.get("placement")) == "1" and isinstance(row.get("opponentname"), str)
             and row["opponentname"].strip()}
    return names.pop() if len(names) == 1 and len(rows) < 100 else None


async def fetch_tournament_preview(profile: PreviewProfile, *, now: datetime | None = None,
                                   allow_draft: bool = False) -> TournamentPreview:
    now = now or datetime.now(timezone.utc)
    check_profile(profile, now, allow_draft=allow_draft)
    data = await _liquipedia_json("/tournament", {
        "conditions": f"[[pagename::{profile.liquipedia_page}]]", "query": PASSPORT_QUERY,
        "limit": 2, "offset": 0,
    })
    rows = _result(data)
    if len(rows) != 1 or rows[0].get("pagename") != profile.liquipedia_page:
        raise PreviewUnavailable("tournament_identity_ambiguous")
    row = rows[0]
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PreviewUnavailable("tournament_name_missing")
    start, end = _date(row.get("startdate")), _date(row.get("enddate"))
    pool = _integer(row.get("prizepool"), "prize_pool_missing_or_invalid")
    places = _locations(row.get("locations"), profile.venue_scope)
    count = (_integer(row["participantsnumber"], "participant_count_invalid", 128)
             if row.get("participantsnumber") not in (None, "", 0, "0") else None)
    teams, first, warnings = [], None, (["editorial_draft"] if not profile.approved else [])
    if profile.pandascore_serie_id:
        try:
            teams, first = await _participants(profile.pandascore_serie_id)
            if count is None or len(teams) != count:
                teams = []
                warnings.append("participants_incomplete")
        except Exception:
            warnings.append("participants_unavailable")
    story, story_url = profile.story, str(profile.story_source_url) if profile.story_source_url else None
    if not story:
        previous = row.get("previous")
        if isinstance(previous, str) and previous.strip():
            # Use the same safe page validator as for the current event.
            safe_previous = PreviewProfile.safe_page(previous.strip())
            champion = await _previous_champion(safe_previous)
            if champion:
                story = f"Предыдущий розыгрыш выиграла команда {champion}."
                if teams and any(team.name.casefold() == champion.casefold() for team in teams):
                    story += " Теперь она возвращается защищать титул."
                story_url = liquipedia_url(safe_previous)
    if not story or not story_url:
        raise PreviewUnavailable("verified_story_missing")
    return TournamentPreview(
        key=profile.key, name=name, start=start, end=end, prize_pool_usd=pool,
        prize_money_usd=profile.prize_money_usd, club_reward_usd=profile.club_reward_usd,
        prize_split_kind=profile.prize_split_kind,
        prize_source_url=str(profile.prize_source_url) if profile.prize_source_url else None,
        locations=places, participant_count=count, teams=teams, first_match_at=first,
        story=story, short_story=profile.short_story, story_source_url=story_url, stages=profile.stages,
        passport_source_url=liquipedia_url(profile.liquipedia_page),
        participants_source_url=(f"https://api.pandascore.co/series/{profile.pandascore_serie_id}/tournaments"
                                 if profile.pandascore_serie_id else None),
        verified_at=profile.verified_at, fetched_at=now, warnings=warnings,
        branding=profile.branding,
    )
