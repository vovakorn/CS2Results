"""Result surfaces inherit announcement tones and preserve the approved atlas."""
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from cs2bot import media_cards as media
from cs2bot import schedule_reels, tournament_visuals
from cs2bot.match_sources.models import MapResult, MatchNormalized
from cs2bot.tournament_branding import branding_for_tournament, load_theme_registry
from cs2bot.tournament_preview import TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cover
from cs2bot.tournament_theme_patterns import preview_background


EVENTS = ["PGL Masters Bucharest 2026", "IEM Beijing 2026",
          "BLAST Rivals Hong Kong 2026", "PGL Major Singapore 2026"]
NOW = datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Moscow"))


def result(event):
    return MatchNormalized(source="pandascore", tournament_name=event, competition_key=event,
        team1_name="Vitality", team2_name="NAVI", score1=2, score2=1, best_of=3,
        maps=[MapResult(name=name, score1=a, score2=b) for name, a, b in
              [("Mirage", 13, 9), ("Nuke", 8, 13), ("Dust II", 13, 10)]])


@lru_cache
def announcement_panel(event):
    fixture = Path(__file__).parent / "fixtures/tournament_preview_demo.json"
    preview = TournamentPreview.model_validate_json(fixture.read_text())
    preview = preview.model_copy(update={"name": event, "branding": branding_for_tournament(event)})
    with Image.open(BytesIO(render_preview_cover(preview, demo=True))) as image:
        return image.getpixel((80, 590))[:3]


def test_backgrounds_match_unobstructed_edges_of_the_original_atlas():
    samples = Path(__file__).parents[1] / "docs/design/tournament-atlas-2026-10-03/samples"
    checked = 0
    for theme in load_theme_registry().themes:
        original_path = samples / f"{theme.key}.png"
        if not original_path.exists():
            continue
        brand = branding_for_tournament(EVENTS[0]).model_copy(update={
            "accent": theme.accent, "secondary": theme.secondary,
            "background": theme.background, "motif": theme.motif})
        current = preview_background((600, 480), brand, frame=False, header=False).convert("RGB")
        with Image.open(original_path) as original:
            original = original.convert("RGB")
            for box in ((0, 0, 20, 480), (580, 0, 600, 480)):
                assert current.crop(box).tobytes() == original.crop(box).tobytes(), theme.key
        checked += 1
    assert checked == 16


@pytest.mark.parametrize("event", EVENTS)
def test_schedule_reel_keeps_the_atlas_glow_without_a_second_overlay(event):
    brand = branding_for_tournament(event)
    visual = schedule_reels.ReelTournamentVisual(event, brand, None)
    expected = preview_background(schedule_reels.REEL_SIZE, brand, frame=False, header=False)
    actual = schedule_reels._base(visual)
    # This strip is outside the Reel frame, logos, watermark and all text.
    strip = (0, 0, 1080, 210)
    assert actual.crop(strip).tobytes() == expected.crop(strip).tobytes()


@pytest.mark.parametrize("with_brand", [False, True])
def test_schedule_reel_without_an_atlas_keeps_its_existing_glow(with_brand):
    brand = branding_for_tournament(EVENTS[0]).model_copy(update={"background": None}) if with_brand else None
    visual = schedule_reels.ReelTournamentVisual("Fallback", brand, None)
    expected = schedule_reels._base()
    tournament_visuals.add_event_glow(expected, visual.accent)
    actual = schedule_reels._base(visual)
    strip = (0, 0, 1080, 210)
    assert actual.crop(strip).tobytes() == expected.crop(strip).tobytes()


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("kind", ["result", "result_without_maps", "digest1", "digest4", "digest10", "final"])
def test_score_and_map_surfaces_use_the_announcement_panel(event, kind, monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)
    match = result(event)
    expected = announcement_panel(event)
    if kind == "final":
        match = match.model_copy(update={"source": "liquipedia", "is_final": True,
            "final_identity_confirmed": True, "winner_prize_usd": 100000})
        pages = [media.render_final_card(match)]
    elif kind.startswith("digest"):
        count = int(kind.removeprefix("digest"))
        pages = media.render_results_cards([match.model_copy(update={"match_id": str(i)})
                                            for i in range(count)], NOW)
        assert len(pages) == (count + 3) // 4
    else:
        if kind == "result_without_maps":
            match = match.model_copy(update={"maps": []})
        pages = [media.render_result_card(match)]
    for index, data in enumerate(pages):
        with Image.open(BytesIO(data)) as image:
            assert image.size == (1080, 1080)
            if kind == "final":
                assert image.getpixel((205, 550))[:3] == expected
            elif kind.startswith("digest") and int(kind.removeprefix("digest")) > 1:
                count_on_page = min(4, int(kind.removeprefix("digest")) - index * 4)
                for x0, y0, x1, y1 in media._fixture_page_boxes(count_on_page):
                    assert image.getpixel((x0 + 20, y1 - 25))[:3] == expected
            else:
                actual = image.getpixel((80, 620))[:3]
                brand = branding_for_tournament(event)
                accent = tuple(int(brand.accent[i:i + 2], 16) for i in (1, 3, 5))
                assert max(abs(a - b) for a, b in zip(actual, expected)) <= 16
                assert all(min(b, c) <= a <= max(b, c) for a, b, c in zip(actual, expected, accent))
                if kind != "result_without_maps":
                    assert image.getpixel((80, 950))[:3] == expected


def test_mixed_digest_keeps_each_tournaments_panel_color(monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)
    matches = [result(EVENTS[0]), result(EVENTS[3])]
    with Image.open(BytesIO(media.render_results_card(matches, NOW))) as image:
        for match, (x0, y0, x1, y1) in zip(matches, media._fixture_page_boxes(2)):
            assert image.getpixel((x0 + 20, y1 - 25))[:3] == announcement_panel(match.tournament_name)


def test_unbranded_result_keeps_channel_panel(monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)
    with Image.open(BytesIO(media.render_result_card(result("Unknown Community Cup")))) as image:
        assert image.getpixel((80, 620))[:3] == media.PANEL


@pytest.mark.parametrize("event", EVENTS)
def test_result_panel_has_a_subtle_gradient_and_themed_outline(event, monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)
    base = announcement_panel(event)
    with Image.open(BytesIO(media.render_result_card(result(event)))) as image:
        top = image.getpixel((80, 450))[:3]
        bottom = image.getpixel((80, 790))[:3]
        edge = image.getpixel((60, 620))[:3]
    assert bottom == base
    top_distance = sum(abs(a - b) for a, b in zip(top, base))
    edge_distance = sum(abs(a - b) for a, b in zip(edge, base))
    assert 0 < top_distance < edge_distance
    assert max(abs(a - b) for a, b in zip(top, base)) <= 16
