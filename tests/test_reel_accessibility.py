"""Contrast and safe-bounds regressions on the rendered text underlays."""
from datetime import datetime

from PIL import ImageChops
import pytest

from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.tournament_branding import branding_for_tournament
from cs2bot.tournament_theme_patterns import preview_background
from scripts.audit_reel_accessibility import audit
from scripts.reel_demo_fixtures import demo_fixtures

NOW = datetime.fromisoformat("2026-10-28T09:00:00+03:00")


@pytest.mark.parametrize("event", ["PGL Masters Bucharest", "PGL Major Singapore 2026", "ESL Pro League Season 24"])
def test_intro_text_exceeds_aa_normal_text_contrast_inside_safe_bounds(event, monkeypatch):
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: None)
    fixtures = demo_fixtures(4, event)
    for scene in r.storyboard(fixtures):
        if scene.kind != "intro":
            continue
        _, records = audit(r.render_scene, scene, fixtures, NOW)
        assert records
        assert all(row["contrast_pass"] for row in records), [row for row in records if not row["contrast_pass"]]
        assert all(row["safe_bounds_pass"] for row in records)


def test_diagonal_opacity_is_halved_without_changing_default_atlas():
    brand = branding_for_tournament("PGL Masters Bucharest")
    options = {"frame": False, "header": False}
    empty = preview_background((600, 480), brand, motif_opacity=0, **options).convert("RGB")
    full = preview_background((600, 480), brand, **options).convert("RGB")
    half = preview_background((600, 480), brand, motif_opacity=.5, **options).convert("RGB")
    full_max = max(high for low, high in ImageChops.difference(full, empty).getextrema())
    half_max = max(high for low, high in ImageChops.difference(half, empty).getextrema())
    assert full_max > 0 and 0 < half_max <= full_max / 2 + 1
