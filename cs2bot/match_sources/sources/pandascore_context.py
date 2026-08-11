from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ..models import (
    MatchNormalized,
    ScheduleMatchContext,
    SourceUnavailableError,
    TeamForm,
    TournamentRadar,
    UpcomingMatchNormalized,
)
from . import pandascore_source


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _team_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name") or value.get("full_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _team_form(team_name: str, team_id: str | None, matches: list[MatchNormalized]) -> TeamForm:
    wins = 0
    losses = 0
    for match in matches:
        first_id = match.source_refs.team1_id if match.source_refs else None
        second_id = match.source_refs.team2_id if match.source_refs else None
        if team_id and first_id == team_id:
            own, other = match.score1, match.score2
        elif team_id and second_id == team_id:
            own, other = match.score2, match.score1
        elif match.team1_name.casefold() == team_name.casefold():
            own, other = match.score1, match.score2
        elif match.team2_name.casefold() == team_name.casefold():
            own, other = match.score2, match.score1
        else:
            continue
        if own is None or other is None:
            continue
        if own > other:
            wins += 1
        elif own < other:
            losses += 1
    return TeamForm(team_name=team_name, wins=wins, losses=losses)


def _roster_sizes(data: Any, team_ids: set[str]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for item in _walk_dicts(data):
        team = item.get("team")
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        if str(team_id) not in team_ids:
            continue
        players = item.get("players") or item.get("roster")
        if isinstance(players, list):
            sizes[str(team_id)] = len(players)
    return sizes


async def _fetch_team_matches(team_id: str, limit: int = 5) -> list[MatchNormalized]:
    data = await pandascore_source._fetch_json(
        f"/teams/{team_id}/matches",
        {"filter[status]": "finished", "sort": "-end_at", "per_page": limit},
    )
    return pandascore_source._normalize_raw_matches(data)[:limit]


async def fetch_schedule_match_context(match: UpcomingMatchNormalized) -> ScheduleMatchContext:
    refs = match.source_refs
    if not refs or not refs.team1_id or not refs.team2_id:
        raise SourceUnavailableError("PandaScore context requires team IDs")

    requests: list[Any] = [
        _fetch_team_matches(refs.team1_id),
        _fetch_team_matches(refs.team2_id),
    ]
    if refs.tournament_id:
        requests.append(pandascore_source._fetch_json(f"/tournaments/{refs.tournament_id}/rosters", {}))
    responses = await asyncio.gather(*requests)
    team1_matches = responses[0]
    team2_matches = responses[1]
    roster_data = responses[2] if len(responses) > 2 else []
    sizes = _roster_sizes(roster_data, {refs.team1_id, refs.team2_id})
    return ScheduleMatchContext(
        match_id=match.match_id,
        tournament_id=refs.tournament_id,
        team1_form=_team_form(match.team1_name, refs.team1_id, team1_matches),
        team2_form=_team_form(match.team2_name, refs.team2_id, team2_matches),
        team1_roster_size=sizes.get(refs.team1_id),
        team2_roster_size=sizes.get(refs.team2_id),
    )


def _standing_lines(data: Any, limit: int = 8) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in _walk_dicts(data):
        team_name = _team_name(item.get("team"))
        rank = item.get("rank") or item.get("position")
        if not team_name or not isinstance(rank, int):
            continue
        line = f"{rank}. {team_name}"
        if line not in seen:
            lines.append(line)
            seen.add(line)
        if len(lines) >= limit:
            break
    return sorted(lines, key=lambda value: int(value.split(".", 1)[0]))


def _bracket_match_count(data: Any) -> int:
    """Count distinct bracket matches without relying on the response nesting."""
    match_ids: set[str] = set()
    for item in _walk_dicts(data):
        match = item.get("match")
        match_id = item.get("match_id")
        if isinstance(match, dict):
            match_id = match.get("id", match_id)
        if isinstance(match_id, (int, str)) and str(match_id).strip():
            match_ids.add(str(match_id))
    return len(match_ids)


def _roster_team_count(data: Any) -> int:
    ids = {
        str(item["team"]["id"])
        for item in _walk_dicts(data)
        if isinstance(item.get("team"), dict) and item["team"].get("id") is not None
    }
    return len(ids)


async def fetch_tournament_radar(tournament_id: str) -> TournamentRadar:
    bracket, standings, rosters = await asyncio.gather(
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/brackets", {}),
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/standings", {}),
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/rosters", {}),
    )
    return TournamentRadar(
        tournament_id=tournament_id,
        standings=_standing_lines(standings),
        roster_team_count=_roster_team_count(rosters),
        bracket_match_count=_bracket_match_count(bracket),
    )
