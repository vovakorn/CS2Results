"""Local branding, complete map data and animation invariants."""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
from cs2bot.match_sources.models import (MapResult, MatchNormalized, UpcomingMatchNormalized,
                                        SourceReferences, TournamentVRSImpact)
from cs2bot.tournament_preview import load_profiles
from cs2bot.tournament_visuals import branding_for_matches

NOW = datetime(2026, 10, 3, 9, tzinfo=ZoneInfo("Europe/Moscow"))


def event_match(index=0):
    profile = load_profiles(TOURNAMENT_PREVIEW_PROFILES_PATH)[0]
    return UpcomingMatchNormalized(match_id=str(index), tournament_name="ESL Pro League Season 24",
        competition_key="ESL Pro League Season 24", team1_name="Team Spirit",
        team2_name="FUT Esports", scheduled_at=f"2026-10-03T{10 + index:02d}:00:00+03:00",
        best_of=3, source_refs=SourceReferences(serie_id=str(profile.pandascore_serie_id)))


def result_with_maps(count):
    wins = min(count, 2 if count <= 3 else 3)
    return MatchNormalized(source="pandascore", match_id="result", tournament_name="Test",
        team1_name="Team Spirit", team2_name="FUT Esports", score1=wins, score2=count - wins,
        best_of=5 if count >= 4 else 3 if count >= 2 else 1,
        maps=[MapResult(name=name, score1=13 if i < wins else 9, score2=8 if i < wins else 13)
              for i, name in enumerate(["Mirage", "Nuke", "Ancient", "Dust II", "Inferno"][:count])])


def test_page_brand_requires_every_match_to_have_the_same_exact_event():
    matches = [event_match(), event_match(1)]
    assert branding_for_matches(matches) is not None
    unknown = event_match(1).model_copy(update={"source_refs": None})
    assert branding_for_matches([matches[0], unknown]) is None
    assert branding_for_matches([]) is None


@pytest.mark.parametrize("kind", ["schedule", "digest", "context"])
def test_registered_event_uses_local_logo_and_green_stripes_without_network(kind, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Registered tournament must use its bundled logo")
    monkeypatch.setattr(m, "fetch_team_logo", no_network)
    fixtures = [event_match(), event_match(1)]
    if kind == "schedule":
        data = m.render_schedule_cards(fixtures, NOW, "Europe/Moscow")[0]
    elif kind == "context":
        data = m.render_schedule_context_covers(fixtures, NOW)[0]
    else:
        results = [MatchNormalized(source="pandascore", match_id=match.match_id,
            tournament_name=match.tournament_name, competition_key=match.competition_key,
            source_refs=match.source_refs, team1_name=match.team1_name, team2_name=match.team2_name,
            score1=2, score2=1) for match in fixtures]
        data = m.render_results_card(results, NOW)
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG" and image.size == (1080, 1080)
    for x in (80, 1000):
        red, green, blue = image.getpixel((x, 90))
        assert green > red * 2 and green > blue * 2


def test_mixed_tournament_page_keeps_channel_palette():
    first = event_match()
    second = event_match(1).model_copy(update={"competition_key": "Other", "tournament_name": "Other"})
    image = Image.open(io.BytesIO(m.render_schedule_card([first, second], NOW, "Europe/Moscow")))
    assert m._fixture_brand([first, second]) is None
    left, right = image.getpixel((80, 90)), image.getpixel((1000, 90))
    assert left[2] > left[0] and right[0] > right[2]


def test_context_replaces_illustration_and_retains_later_available_event_logo(monkeypatch):
    first = event_match().model_copy(update={"source_refs": None})
    second = event_match(1).model_copy(update={"source_refs": None, "tournament_logo_url": "preview://event"})
    assert m._schedule_tournament_header([first, second])[1] == "preview://event"
    loaded = []
    monkeypatch.setattr(m, "fetch_team_logo",
                        lambda url: (loaded.append(url), Image.new("RGBA", (100, 100), "white"))[1])
    monkeypatch.setattr(m, "SCHEDULE_CONTEXT_BACKGROUND", "nonexistent-illustration.png")
    assert Image.open(io.BytesIO(m.render_schedule_context_covers([first, second], NOW)[0])).size == (1080, 1080)
    assert loaded == ["preview://event"]


def test_missing_bundled_logo_uses_provider_mark_before_plain_title(monkeypatch):
    from cs2bot import tournament_visuals
    loaded = []
    monkeypatch.setattr(tournament_visuals, "event_logo", lambda *args: None)
    monkeypatch.setattr(m, "fetch_team_logo",
        lambda url: (loaded.append(url), Image.new("RGBA", (100, 100), "white"))[1])
    match = event_match().model_copy(update={"tournament_logo_url":"preview://event"})
    assert Image.open(io.BytesIO(m.render_schedule_context_covers([match], NOW)[0])).size == (1080, 1080)
    assert loaded == ["preview://event"]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_single_result_retains_every_confirmed_map(count, monkeypatch):
    texts = []
    original = m._draw_text_block
    def capture(draw, x, y, text, *args, **kwargs):
        texts.append(text)
        return original(draw, x, y, text, *args, **kwargs)
    monkeypatch.setattr(m, "_draw_text_block", capture)
    match = result_with_maps(count)
    assert m._confirmed_result_maps(match) == match.maps
    image = Image.open(io.BytesIO(m.render_result_card(match)))
    assert image.size == (1080, 1080)
    assert all(item.name.upper() in texts for item in match.maps)
    assert all(f"{item.score1}:{item.score2}" in texts for item in match.maps)


@pytest.mark.parametrize("case", ["missing_score", "tied", "partial", "forfeit", "walkover"])
def test_incomplete_or_awarded_map_data_is_not_displayed(case):
    match = result_with_maps(3)
    if case == "missing_score":
        match.maps[0].score1 = None
    elif case == "tied":
        match.maps[0].score2 = match.maps[0].score1
    elif case == "partial":
        match.maps = match.maps[:2]
    elif case == "forfeit":
        match.forfeit = True
    else:
        match.result_type = "walkover"
    assert m._confirmed_result_maps(match) == []


def test_map_strip_does_not_override_a_contradictory_best_of():
    match = result_with_maps(5)
    match.best_of = 3
    assert m._confirmed_result_maps(match) == []


def test_vrs_all_pages_use_brand_and_draw_both_absolute_ranks(monkeypatch):
    profile = load_profiles(TOURNAMENT_PREVIEW_PROFILES_PATH)[0]
    impacts = [TournamentVRSImpact(placement=str(i + 1), team_name=f"Team {i}", team_id=str(i),
        before_points=1000, after_points=1020, before_rank=i + 20, after_rank=i + 10,
        points_delta=20, rank_delta=10, source="Valve VRS", before_version="before",
        after_version="after") for i in range(16)]
    transitions = []
    original = m._draw_rank_transition
    def capture(draw, x, y, before, after, color):
        transitions.append((before, after))
        original(draw, x, y, before, after, color)
    monkeypatch.setattr(m, "_draw_rank_transition", capture)
    pages = m.render_tournament_vrs_cards("ESL Pro League Season 24", impacts, branding=profile.branding)
    assert len(pages) == 2
    assert transitions == [(item.before_rank, item.after_rank) for item in impacts]
    for data in pages:
        image = Image.open(io.BytesIO(data))
        assert image.size == (1080, 1080)
        assert image.getpixel((80, 90))[1] > image.getpixel((80, 90))[2] * 2


def test_reel_intro_uses_full_day_without_duplicating_storyboard_data():
    scenes = r.storyboard([event_match(1), event_match()])
    assert scenes[0].matches == ()
    assert [match.match_id for match in scenes[0].context_matches] == ["0", "1"]
    assert [match.match_id for scene in scenes for match in scene.matches] == ["0", "1"]
    assert r.render_scene(scenes[0], NOW, 2).size == (1080, 1920)


def test_reel_animation_is_staggered_and_progress_reaches_end():
    assert r._reveal_opacity(.2, 0) > r._reveal_opacity(.2, 1)
    assert r._reveal_opacity(.75, 3) == 1
    image = Image.new("RGB", (1080, 1920), "black")
    r._progress(image, 1)
    assert image.getpixel((978, 1677)) == r.CYAN
    scene = r.ReelScene("matches", (event_match(),), .25)
    frames = list(r.animated_scene_frames(scene, NOW, 1, "Europe/Moscow",
        elapsed_before=0, total_seconds=.25))
    assert len(frames) == 3
    assert sum(duration for _, duration in frames) == pytest.approx(.25)
    assert frames[0][0].tobytes() != frames[-1][0].tobytes()
