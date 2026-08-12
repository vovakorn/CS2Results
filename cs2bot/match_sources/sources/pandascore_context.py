from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ..models import (
    MatchNormalized,
    HeadToHead,
    RadarBracketMatch,
    ScheduleMatchContext,
    SourceUnavailableError,
    RadarStandingTeam,
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


def _head_to_head(
    team1_id: str,
    team2_id: str,
    matches: list[MatchNormalized],
    limit: int = 3,
) -> HeadToHead | None:
    """Build a recent H2H record from one team's own match history."""
    team1_wins = 0
    team2_wins = 0
    counted = 0
    for match in matches:
        refs = match.source_refs
        if not refs or {refs.team1_id, refs.team2_id} != {team1_id, team2_id}:
            continue
        if refs.team1_id == team1_id:
            first_score, second_score = match.score1, match.score2
        else:
            first_score, second_score = match.score2, match.score1
        if first_score is None or second_score is None or first_score == second_score:
            continue
        if first_score > second_score:
            team1_wins += 1
        else:
            team2_wins += 1
        counted += 1
        if counted >= limit:
            break
    if counted < 2:
        return None
    return HeadToHead(match_count=counted, team1_wins=team1_wins, team2_wins=team2_wins)


async def fetch_schedule_match_context(match: UpcomingMatchNormalized) -> ScheduleMatchContext:
    refs = match.source_refs
    if not refs or not refs.team1_id or not refs.team2_id:
        raise SourceUnavailableError("PandaScore context requires team IDs")

    requests: list[Any] = [
        _fetch_team_matches(refs.team1_id, limit=20),
        _fetch_team_matches(refs.team2_id, limit=20),
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
        team1_form=_team_form(match.team1_name, refs.team1_id, team1_matches[:5]),
        team2_form=_team_form(match.team2_name, refs.team2_id, team2_matches[:5]),
        head_to_head=_head_to_head(refs.team1_id, refs.team2_id, team1_matches),
        team1_roster_size=sizes.get(refs.team1_id),
        team2_roster_size=sizes.get(refs.team2_id),
    )


def _standing_teams(data: Any, limit: int = 8) -> list[RadarStandingTeam]:
    teams: list[RadarStandingTeam] = []
    seen: set[str] = set()
    for item in _walk_dicts(data):
        team_name = _team_name(item.get("team"))
        rank = item.get("rank") or item.get("position")
        if not team_name or not isinstance(rank, int):
            continue
        key = f"{rank}:{team_name.casefold()}"
        if key not in seen:
            teams.append(
                RadarStandingTeam(
                    rank=rank,
                    name=team_name,
                    logo_url=item["team"].get("image_url") if isinstance(item["team"].get("image_url"), str) else None,
                )
            )
            seen.add(key)
        if len(teams) >= limit:
            break
    return sorted(teams, key=lambda team: team.rank)


def _standing_lines(data: Any, limit: int = 8) -> list[str]:
    return [f"{team.rank}. {team.name}" for team in _standing_teams(data, limit)]


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


def _bracket_matches(data: Any, limit: int = 4) -> list[RadarBracketMatch]:
    """Extract only explicit pairs; never infer a playoff matchup from standings."""
    matches: list[RadarBracketMatch] = []
    seen: set[str] = set()
    for item in _walk_dicts(data):
        raw_match = item.get("match") if isinstance(item.get("match"), dict) else item
        match_id = raw_match.get("id") or item.get("match_id")
        opponents = raw_match.get("opponents")
        teams: list[dict[str, Any]] = []
        if isinstance(opponents, list):
            teams = [entry.get("opponent", entry) for entry in opponents if isinstance(entry, dict)]
        elif isinstance(raw_match.get("teams"), list):
            teams = [team for team in raw_match["teams"] if isinstance(team, dict)]
        elif isinstance(raw_match.get("team1"), dict) and isinstance(raw_match.get("team2"), dict):
            teams = [raw_match["team1"], raw_match["team2"]]
        names = [_team_name(team) for team in teams]
        if not isinstance(match_id, (str, int)) or len(names) < 2 or not names[0] or not names[1]:
            continue
        key = str(match_id)
        if key in seen:
            continue
        round_value = item.get("round") or item.get("round_name") or item.get("name")
        matches.append(
            RadarBracketMatch(
                match_id=key,
                round_name=round_value if isinstance(round_value, str) else None,
                team1_name=names[0],
                team2_name=names[1],
                status=raw_match.get("status") if isinstance(raw_match.get("status"), str) else None,
            )
        )
        seen.add(key)
        if len(matches) >= limit:
            break
    return matches


def _roster_team_count(data: Any) -> int:
    ids = {
        str(item["team"]["id"])
        for item in _walk_dicts(data)
        if isinstance(item.get("team"), dict) and item["team"].get("id") is not None
    }
    return len(ids)


async def fetch_tournament_radar(tournament_id: str) -> TournamentRadar:
    responses = await asyncio.gather(
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/brackets", {}),
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/standings", {}),
        pandascore_source._fetch_json(f"/tournaments/{tournament_id}/rosters", {}),
        pandascore_source._fetch_json(
            f"/tournaments/{tournament_id}/matches",
            {"filter[status]": "not_started", "sort": "begin_at", "per_page": 4},
        ),
        return_exceptions=True,
    )
    bracket, standings, rosters, matches = [value if not isinstance(value, Exception) else [] for value in responses]
    if all(isinstance(value, Exception) for value in responses):
        raise SourceUnavailableError("PandaScore tournament radar endpoints are unavailable")
    standing_teams = _standing_teams(standings)
    return TournamentRadar(
        tournament_id=tournament_id,
        standings=[f"{team.rank}. {team.name}" for team in standing_teams],
        standing_teams=standing_teams,
        bracket_matches=_bracket_matches(bracket),
        next_matches=pandascore_source._normalize_raw_upcoming(matches)[:4],
        roster_team_count=_roster_team_count(rosters),
        bracket_match_count=_bracket_match_count(bracket),
    )
