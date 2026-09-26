import asyncio
import io

import pytest
from PIL import Image

from cs2bot.match_sources.models import TournamentPlacement, TournamentVRSImpact
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
    assert all(item.before_effective_at == before.effective_at for item in impacts)
    assert all(item.after_effective_at == after.effective_at for item in impacts)


def test_vrs_card_renders_snapshot_dates_and_change_signs(monkeypatch):
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
        TournamentPlacement(placement="1", team_name="NAVI"),
        TournamentPlacement(placement="2", team_name="FaZe"),
        TournamentPlacement(placement="3", team_name="Spirit"),
    ], before, after)
    drawn = []
    arrows = []
    original = media_cards._centered_text
    original_arrow = media_cards._draw_vrs_rank_arrow

    def capture_text(draw, center_x, y, text, font, fill):
        drawn.append((text, font))
        return original(draw, center_x, y, text, font, fill)

    def capture_arrow(draw, center_x, center_y, *, up, color):
        arrows.append((up, color))
        return original_arrow(draw, center_x, center_y, up=up, color=color)

    monkeypatch.setattr(media_cards, "_centered_text", capture_text)
    monkeypatch.setattr(media_cards, "_draw_vrs_rank_arrow", capture_arrow)
    card = media_cards.render_tournament_vrs_cards("Test Tournament", impacts)[0]
    assert Image.open(io.BytesIO(card)).size == (1080, 1080)
    rendered_text = {text for text, _ in drawn}
    assert "VRS ПОСЛЕ ТУРНИРА" in rendered_text
    assert "ДО 01.09.2026  |  ПОСЛЕ 08.09.2026" in rendered_text
    assert {"+42", "-18", "0", "3", "2"} <= rendered_text
    assert arrows == [(True, media_cards.VRS_UP), (False, media_cards.VRS_DOWN)]
    for text, font in drawn:
        for sign in ("+", "-"):
            if sign in text:
                assert font.getmask(sign).getbbox() is not None
        if text.startswith("ДО "):
            assert all(font.getmask(letter).getbbox() is not None for letter in "ДОПОСЛЕ")


def test_vrs_card_uses_version_dates_for_older_queued_impacts():
    legacy = TournamentVRSImpact.model_validate({
        "placement": "1",
        "team_name": "NAVI",
        "team_id": "navi",
        "before_points": 100,
        "after_points": 142,
        "before_rank": 10,
        "after_rank": 7,
        "points_delta": 42,
        "rank_delta": 3,
        "source": "official-vrs",
        "before_version": "standings_global_2026_09_01.md:abc",
        "after_version": "standings_global_2026_09_08.md:def",
    })
    assert legacy.before_effective_at is None
    assert legacy.after_effective_at is None
    assert media_cards.can_render_tournament_vrs([legacy])
    assert media_cards._vrs_snapshot_date(None, "standings_global_2026_09_01.md:abc") == "01.09.2026"
    assert media_cards._vrs_snapshot_date(None, "standings_global_2026_09_08.md:def") == "08.09.2026"
    assert media_cards._vrs_snapshot_date(None, "week-1") is None


def test_vrs_caption_describes_timing_without_claiming_causality():
    from cs2bot.main import format_tournament_vrs

    before = _snapshot("week-1", "2026-09-01T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 100, "rank": 10},
    ])
    after = _snapshot("week-2", "2026-09-08T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 142, "rank": 7},
    ])
    impacts = calculate_impacts([TournamentPlacement(placement="1", team_name="NAVI")], before, after)
    caption = format_tournament_vrs("Test Tournament", impacts)
    assert "VRS после турнира — Test Tournament" in caption
    assert "Влияние турнира на VRS" not in caption


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
