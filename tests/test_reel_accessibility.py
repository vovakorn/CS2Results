"""Contrast and safe-bounds regressions on the rendered text underlays."""
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw
import pytest

from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.tournament_branding import branding_for_tournament
from cs2bot.tournament_theme_patterns import preview_background
from scripts.audit_reel_accessibility import audit
from scripts.preview_reel_safety_zone import demo_fixtures, render as safety_render

NOW = datetime.fromisoformat("2026-10-28T09:00:00+03:00")


@pytest.mark.parametrize("event", ["PGL Masters Bucharest", "PGL Major Singapore 2026", "ESL Pro League Season 24"])
@pytest.mark.parametrize("render", [r.render_scene, safety_render])
def test_intro_text_exceeds_aa_normal_text_contrast_inside_safe_bounds(event, render, monkeypatch):
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: None)
    fixtures = demo_fixtures(4, event)
    for scene in r.storyboard(fixtures):
        if scene.kind != "intro":
            continue
        _, records = audit(render, scene, fixtures, NOW)
        assert records
        assert all(row["contrast_pass"] for row in records), [row for row in records if not row["contrast_pass"]]
        assert all(row["safe_bounds_pass"] for row in records)


def test_safety_variant_removes_outer_frame_and_left_aligns_body(monkeypatch):
    rectangles, blocks = [], []
    original_rectangle, original_block = ImageDraw.ImageDraw.rounded_rectangle, r._draw_text_block
    def rectangle(draw, xy, *args, **kwargs):
        if draw._image.size == r.REEL_SIZE:
            rectangles.append(tuple(xy))
        return original_rectangle(draw, xy, *args, **kwargs)
    def block(draw, x, y, text, width, size=38, **kwargs):
        blocks.append((text, x, y, kwargs.get("alignment", "center")))
        return original_block(draw, x, y, text, width, size, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", rectangle)
    monkeypatch.setattr(r, "_draw_text_block", block)
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: None)
    fixtures = demo_fixtures()
    safety_render(r.storyboard(fixtures)[0], NOW, 4)
    assert (72, 230, 928, 1590) not in rectangles
    assert all(alignment == "left" for _, x, y, alignment in blocks if y > 400)
    assert all(x == 96 for _, x, y, _ in blocks if y > 400 and not 814 <= y <= 1366)


def test_diagonal_opacity_is_halved_without_changing_default_atlas():
    brand = branding_for_tournament("PGL Masters Bucharest")
    options = {"frame": False, "header": False}
    empty = preview_background((600, 480), brand, motif_opacity=0, **options).convert("RGB")
    full = preview_background((600, 480), brand, **options).convert("RGB")
    half = preview_background((600, 480), brand, motif_opacity=.5, **options).convert("RGB")
    full_max = max(high for low, high in ImageChops.difference(full, empty).getextrema())
    half_max = max(high for low, high in ImageChops.difference(half, empty).getextrema())
    assert full_max > 0 and 0 < half_max <= full_max / 2 + 1
