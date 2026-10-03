import asyncio
import io

import pytest
from PIL import Image, ImageDraw

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


def _valve_rosters():
    # Actual collision in Valve live/global 2026-09-07: two different rosters.
    return [
        "| 106 | 911 | Johnny Speeds | bsover, draken, hampus, Rack, Svedjehed | [details](details/2026_09_07/0106--johnny_speeds--one.md) |",
        "| 111 | 884 | Johnny Speeds | HEAP, jocab, Lekr0, nawwk, titulus | [details](details/2026_09_07/0111--johnny_speeds--two.md) |",
        "| 1 | 2031 | Spirit | A, B, C, D, E | [details](details/2026_09_07/0001--spirit--players.md) |",
    ]


def test_valve_distinct_rosters_share_a_name_without_losing_rows_or_unique_ids():
    rows = _valve_rosters()
    before = _parse_snapshot("\n".join(rows), effective_at="2026-09-07T00:00:00Z", version="before")
    reordered = [rows[1].replace("HEAP, jocab, Lekr0, nawwk, titulus", "titulus, NAWWK, Lekr0, jocab, HEAP"),
                 rows[0].replace("106", "107"), rows[2]]
    after = _parse_snapshot("\n".join(reordered), effective_at="2026-09-08T00:00:00Z", version="after")
    assert len(before.teams) == 3 and len({team.team_id for team in before.teams}) == 3
    assert before.teams[2].team_id == "spirit"
    assert {team.points: team.team_id for team in before.teams} == {team.points: team.team_id for team in after.teams}
    assert calculate_impacts([TournamentPlacement(placement="1", team_name="Spirit")], before, after)[0].points_delta == 0


@pytest.mark.parametrize("collision_phase", ["before", "after"])
def test_vrs_impact_rejects_ambiguous_tournament_team_even_if_other_names_are_unique(collision_phase):
    duplicate = _parse_snapshot("\n".join(_valve_rosters()), effective_at="2026-09-07T00:00:00Z", version="first")
    unique = duplicate.model_copy(update={"teams": [duplicate.teams[0], duplicate.teams[2]]})
    before = duplicate if collision_phase == "before" else unique
    after = (duplicate if collision_phase == "after" else unique).model_copy(update={
        "version": "second", "effective_at": "2026-09-08T00:00:00Z"})
    with pytest.raises(VRSDataError, match="ambiguous"):
        calculate_impacts([TournamentPlacement(placement="1", team_name="Johnny Speeds")], before, after)


def test_valve_can_declare_a_partial_roster_for_one_of_two_same_named_teams():
    rows = _valve_rosters()[:2]
    rows[0] = rows[0].replace("bsover, draken, hampus, Rack, Svedjehed", "h1te, sm3t, Something, sstiNiX")
    snapshot = _parse_snapshot("\n".join(rows), effective_at="2026-09-07T00:00:00Z", version="first")
    assert len(snapshot.teams) == len({team.team_id for team in snapshot.teams}) == 2


@pytest.mark.parametrize("other_row", [
    _valve_rosters()[0],
    _valve_rosters()[0].replace("bsover, draken, hampus, Rack, Svedjehed", ""),
])
def test_valve_repeated_or_missing_roster_still_blocks_snapshot(other_row):
    with pytest.raises(VRSDataError):
        _parse_snapshot(_valve_rosters()[0] + "\n" + other_row, effective_at="2026-09-07T00:00:00Z", version="first")


def test_vrs_calculates_points_and_rank_directions():
    before = _snapshot("week-1", "2026-09-01T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 100, "rank": 10},
        {"id": "b", "name": "FaZe", "points": 200, "rank": 3},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
        {"id": "d", "name": "Vitality", "points": 250, "rank": 6},
    ])
    after = _snapshot("week-2", "2026-09-08T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 142, "rank": 7},
        {"id": "b", "name": "FaZe", "points": 182, "rank": 5},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
        {"id": "d", "name": "Vitality", "points": 250, "rank": 6},
    ])
    impacts = calculate_impacts([
        TournamentPlacement(placement="1", team_name="NAVI", prize_usd=None),
        TournamentPlacement(placement="2", team_name="FaZe", prize_usd=None),
        TournamentPlacement(placement="3", team_name="Spirit", prize_usd=None),
        TournamentPlacement(placement="4", team_name="Vitality", prize_usd=None),
    ], before, after)
    assert [(item.points_delta, item.rank_delta) for item in impacts] == [(42, 3), (-18, -2), (0, 0), (0, 0)]
    assert all(item.before_effective_at == before.effective_at for item in impacts)
    assert all(item.after_effective_at == after.effective_at for item in impacts)


def test_vrs_card_renders_snapshot_dates_and_change_signs(monkeypatch):
    before = _snapshot("week-1", "2026-09-01T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 100, "rank": 10},
        {"id": "b", "name": "FaZe", "points": 200, "rank": 3},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
        {"id": "d", "name": "Vitality", "points": 250, "rank": 6},
    ])
    after = _snapshot("week-2", "2026-09-08T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 142, "rank": 7},
        {"id": "b", "name": "FaZe", "points": 182, "rank": 5},
        {"id": "c", "name": "Spirit", "points": 300, "rank": 4},
        {"id": "d", "name": "Vitality", "points": 250, "rank": 6},
    ])
    impacts = calculate_impacts([
        TournamentPlacement(placement="1", team_name="NAVI"),
        TournamentPlacement(placement="2", team_name="FaZe"),
        TournamentPlacement(placement="3", team_name="Spirit"),
        TournamentPlacement(placement="4", team_name="Vitality"),
    ], before, after)
    drawn = []
    transitions = []
    original = ImageDraw.ImageDraw.text
    original_transition = media_cards._draw_rank_transition

    def capture_text(draw, xy, text, *args, **kwargs):
        drawn.append((text, kwargs.get("font")))
        return original(draw, xy, text, *args, **kwargs)

    def capture_transition(draw, x, y, before, after, color):
        transitions.append((before, after, color))
        return original_transition(draw, x, y, before, after, color)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(media_cards, "_draw_rank_transition", capture_transition)
    card = media_cards.render_tournament_vrs_cards("Test Tournament", impacts)[0]
    assert Image.open(io.BytesIO(card)).size == (1080, 1080)
    rendered_text = {text for text, _ in drawn}
    assert "VRS ПОСЛЕ ТУРНИРА" in rendered_text
    assert "ДО 01.09.2026 · ПОСЛЕ 08.09.2026" in rendered_text
    assert {"+42", "-18", "0", "+3", "-2"} <= rendered_text
    assert transitions == [(10, 7, media_cards.VRS_UP), (3, 5, media_cards.VRS_DOWN),
                           (4, 4, media_cards.MUTED), (6, 6, media_cards.MUTED)]
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
    second = legacy.model_copy(update={"placement": "2", "team_name": "FaZe", "team_id": "faze"})
    assert not media_cards.can_render_tournament_vrs([legacy])
    assert media_cards.can_render_tournament_vrs([legacy, second])
    assert media_cards._vrs_snapshot_date(None, "standings_global_2026_09_01.md:abc") == "01.09.2026"
    assert media_cards._vrs_snapshot_date(None, "standings_global_2026_09_08.md:def") == "08.09.2026"
    assert media_cards._vrs_snapshot_date(None, "week-1") is None


def test_vrs_caption_describes_timing_without_claiming_causality():
    from cs2bot.main import format_tournament_vrs

    before = _snapshot("week-1", "2026-09-01T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 100, "rank": 10},
        {"id": "b", "name": "FaZe", "points": 200, "rank": 3},
    ])
    after = _snapshot("week-2", "2026-09-08T00:00:00Z", [
        {"id": "a", "name": "NAVI", "points": 142, "rank": 7},
        {"id": "b", "name": "FaZe", "points": 190, "rank": 4},
    ])
    impacts = calculate_impacts([
        TournamentPlacement(placement="1", team_name="NAVI"),
        TournamentPlacement(placement="2", team_name="FaZe"),
    ], before, after)
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


@pytest.mark.parametrize("count", [2, 4, 10, 12])
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


def test_vrs_cards_reject_odd_team_count():
    item = TournamentVRSImpact(
        placement="1", team_name="Spirit", team_id="spirit", before_points=100,
        after_points=82, before_rank=5, after_rank=7, points_delta=-18,
        rank_delta=-2, source="official-vrs",
        before_version="standings_global_2026_09_01.md:a",
        after_version="standings_global_2026_09_08.md:b",
    )
    assert not media_cards.can_render_tournament_vrs([item])
    assert not media_cards.can_render_tournament_vrs([item, item, item])
    with pytest.raises(media_cards.MediaCardError, match="complete impacts"):
        media_cards.render_tournament_vrs_cards("BLAST Open Porto", [item])


@pytest.mark.parametrize("count", [2, 4, 6, 8])
def test_vrs_table_uses_available_height(count, monkeypatch):
    panels, footer_tops = [], []
    original = media_cards._chamfered_panel
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        if str(text).startswith("ИСТОЧНИК:"):
            footer_tops.append(draw.textbbox(xy, text, font=kwargs["font"])[1])
        return original_text(draw, xy, text, *args, **kwargs)

    def capture_panel(draw, box, **kwargs):
        panels.append(box)
        return original(draw, box, **kwargs)

    monkeypatch.setattr(media_cards, "_chamfered_panel", capture_panel)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    impacts = [TournamentVRSImpact(
        placement=str(index + 1), team_name=f"Team {index + 1}", team_id=f"team-{index}",
        before_points=100, after_points=142, before_rank=index + 2, after_rank=index + 1,
        points_delta=42, rank_delta=1, source="official-vrs",
        before_version="standings_global_2026_09_01.md:a",
        after_version="standings_global_2026_09_08.md:b",
    ) for index in range(count)]
    media_cards.render_tournament_vrs_cards("BLAST Open Porto", impacts)
    _, top, _, bottom = panels[0]
    assert top >= 340
    assert footer_tops and bottom + 24 <= min(footer_tops)
    assert bottom - top >= (330 if count == 2 else 600)
