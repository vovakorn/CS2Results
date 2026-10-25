"""Verify actual text and emblem bounds, independent of layout coordinates."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
import pytest

from cs2bot import media_cards, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized
from cs2bot.reel_layout import SAFE_BOUNDS, LAYOUT, COVER_OFFSET_MS


def test_safe_zone_is_the_approved_strict_working_area():
    assert SAFE_BOUNDS == (80, 250, 920, 1570)
    assert (LAYOUT["width"], LAYOUT["height"]) == (1080, 1920)
    assert COVER_OFFSET_MS == 500


def inside(box, bounds=SAFE_BOUNDS):
    return all((box[0] >= bounds[0], box[1] >= bounds[1],
                box[2] <= bounds[2], box[3] <= bounds[3]))


@pytest.mark.parametrize("count", [1, 4, 10, 20])
@pytest.mark.parametrize("variant", ["neutral", "branded", "mixed", "long"])
def test_every_scene_keeps_text_and_logos_inside_safe_zone(monkeypatch, count, variant):
    now = datetime(2026, 10, 23, 9, tzinfo=ZoneInfo("Europe/Moscow"))
    fixtures = [UpcomingMatchNormalized(match_id=str(i),
        tournament_name="ESL Pro League Season 24" if variant == "branded" or variant == "mixed" and i % 2 else "Demo event",
        team1_name="Очень длинное название команды для проверки переноса" if variant == "long" else "Gaimin Gladiators Academy",
        team2_name="Natus Vincere Junior", scheduled_at=(now + timedelta(minutes=i * 30)).isoformat(),
        team1_logo_url="preview://light" if i % 2 else None,
        team2_logo_url="preview://dark" if i % 3 else None) for i in range(count)]
    monkeypatch.setattr(media_cards, "fetch_team_logo", lambda url, **kw:
        Image.new("RGBA", (100, 100), "white" if url.endswith("light") else "black"))
    boxes = []
    original_text, original_logo, original_channel = ImageDraw.ImageDraw.text, r._draw_logo, media_cards._draw_channel_logo

    def text(draw, xy, value, *args, **kwargs):
        if value and draw._image.size == r.REEL_SIZE:
            boxes.append((str(value), draw.textbbox(xy, value, font=kwargs.get("font"))))
        return original_text(draw, xy, value, *args, **kwargs)

    def logo(image, draw, center, diameter, *args, **kwargs):
        x, y = center
        radius = diameter / 2
        boxes.append(("team logo", (x-radius, y-radius, x+radius, y+radius)))
        return original_logo(image, draw, center, diameter, *args, **kwargs)

    def channel(image, draw, center, diameter):
        x, y = center
        radius = diameter / 2 + 5
        boxes.append(("channel emblem", (x-radius, y-radius, x+radius, y+radius)))
        return original_channel(image, draw, center, diameter)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(r, "_draw_logo", logo)
    monkeypatch.setattr(media_cards, "_draw_channel_logo", channel)
    for scene in r.storyboard(fixtures):
        boxes.clear()
        assert r.render_scene(scene, now, count).size == (1080, 1920)
        assert boxes
        assert not [(value, box) for value, box in boxes if not inside(box)]
        if scene.kind == "intro":
            cover_bounds = (80, 420, 920, 1500)
            assert all(inside(box, cover_bounds) for value, box in boxes
                       if value in ("ТВОЯ КОМАНДА", "ИГРАЕТ СЕГОДНЯ?"))
