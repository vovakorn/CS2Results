import asyncio
import io

import pytest
from PIL import Image

from cs2bot.match_sources.models import TournamentPlacement
from cs2bot.match_sources.vrs import VRSDataError, calculate_impacts, normalize_snapshot
from cs2bot.match_sources.sources.vrs_source import _parse_snapshot
from cs2bot import media_cards


def _snapshot(version, effective_at, teams):
    return normalize_snapshot(
        {"version": version, "effective_at": effective_at, "teams": teams},
        source="official-vrs",
        fetched_at="2026-09-10T10:00:00Z",
    )


def test_vrs_calculates_points_and_rank_directions():
    before = _snapshot("week-1", "2026-09-01T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 100, "rank": 10},
        {"id": "b", "name": "FaZe", "points": 200, "rank": 3},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
    ])
    after = _snapshot("week-2", "2026-09-08T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 142, "rank": 7},
        {"id": "b", "name": "FaZe", "points": 182, "rank": 5},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
    ])
    impacts = calculate_impacts([
        TournamentPlacement(placement="1", team_name="NAVI", prize_usd=None),
        TournamentPlacement(placement="2", team_name="FaZe", prize_usd=None),
        TournamentPlacement(placement="3", team_name="Spirit", prize_usd=None),
    ], before, after)
    assert [(item.points_delta, item.rank_delta) for item in impacts] == [(42, 3), (-18, -2), (0, 0)]


def test_vrs_rejects_missing_metadata_and_team():
    with pytest.raises(VRSDataError, match="version"):
        normalize_snapshot({"effective_at": "2026-09-01", "teams": []}, source="official-vrs")
    before = _snapshot("1", "2026-09-01T00:00:00Z", [{"id": "a", "name": "NAVI", "points": 1, "rank": 1}])
    after = _snapshot("2", "2026-09-02T00:00:00Z", [{"id": "b", "name": "Other", "points": 1, "rank": 1}])
    with pytest.raises(VRSDataError, match="NAVI"):
        calculate_impacts([TournamentPlacement(placement="1", team_name="NAVI", prize_usd=None)], before, after)


def test_valve_markdown_snapshot_parser_reads_rank_points_and_stable_slug():
    snapshot = _parse_snapshot(
        """### Standings as of 2026_09_07
| Standing | Points | Team Name | Roster |
| :- | -: | :- | :- |
| 1 | 2031 | Spirit | donk, sh1ro |
| 10 | 1599 | FaZe | frozen, Twistzz | [details](details/2026_09_07/0010--faze--frozen-twistzz.md)
""",
        effective_at="2026-09-07T00:00:00Z",
        version="standings_global_2026_09_07.md:blob",
    )
    assert [(item.rank, item.points, item.team_id) for item in snapshot.teams] == [
        (1, 2031, "spirit"),
        (10, 1599, "faze"),
    ]


@pytest.mark.parametrize("count", [1, 4, 10])
def test_vrs_cards_are_square_and_paginated(count):
    before = _snapshot("1", "2026-09-01T00:00:00Z", [
        {"id": str(i), "name": ("A Very Long Team Name " * 4).strip() if i == 0 else f"Team {i}", "points": 100, "rank": i + 1} for i in range(count)
    ])
    after = _snapshot("2", "2026-09-08T00:00:00Z", [
        {"id": str(i), "name": ("A Very Long Team Name " * 4).strip() if i == 0 else f"Team {i}",
         "points": 100 + i, "rank": i + 1} for i in range(count)
    ])
    impacts = calculate_impacts([
        TournamentPlacement(placement=str(i + 1), team_name=("A Very Long Team Name " * 4).strip() if i == 0 else f"Team {i}", prize_usd=None)
        for i in range(count)
    ], before, after)
    cards = media_cards.render_tournament_vrs_cards("Test Tournament", impacts)
    assert len(cards) == (count + media_cards.TOURNAMENT_VRS_PER_CARD - 1) // media_cards.TOURNAMENT_VRS_PER_CARD
    assert all(Image.open(io.BytesIO(card)).size == (1080, 1080) for card in cards)
