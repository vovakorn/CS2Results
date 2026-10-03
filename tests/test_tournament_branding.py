import io
import json
from pathlib import Path

from PIL import Image
import pytest
from pydantic import ValidationError

from cs2bot.tournament_branding import ThemeRegistry, load_theme_registry, resolve_branding
from cs2bot.tournament_preview import PreviewBranding, PreviewProfile, PreviewUnavailable, TournamentPreview, check_profile
from cs2bot.tournament_preview_cards import render_preview_cards


def test_exact_hierarchy_and_unknown_series_never_borrow_an_iem_or_open_mark():
    assert resolve_branding(PreviewBranding(series_key="iem", organizer_key="esl"),
                            event_key="iem-beijing-2026").theme_key == "iem-beijing"
    assert resolve_branding(PreviewBranding(theme_key="cac", series_key="iem"),
                            event_key="iem-beijing-2026").theme_key == "cac"
    assert resolve_branding(PreviewBranding(series_key="blast-open", organizer_key="blast")).logo_asset == "blast-open.png"
    for organizer in ("esl", "blast", "perfect-world"):
        assert resolve_branding(PreviewBranding(series_key="unannounced", organizer_key=organizer)).logo_asset is None
    fallback = resolve_branding(PreviewBranding(organizer_key="unknown"))
    assert fallback.accent == "#16C7FF" and fallback.logo_asset is None and fallback.background is None


def test_explicit_event_visuals_and_headline_survive_inheritance_and_snapshot_replay():
    brand = PreviewBranding(series_key="iem", accent="#123456", logo_asset="custom-event.png",
                            logo_source_url="https://example.com/logo.png", headline="Проверенная история",
                            headline_source_url="https://example.com/history")
    resolved = resolve_branding(brand)
    assert resolved.accent == "#123456" and resolved.logo_asset == "custom-event.png"
    assert resolved.headline == brand.headline and resolved.headline_source_url == brand.headline_source_url
    assert resolved.secondary == "#D7E6F5" and resolved.theme_source_urls
    assert resolve_branding(PreviewBranding.model_validate_json(resolved.model_dump_json())) == resolved


def test_visual_approval_never_enables_an_editorial_profile():
    root = Path(__file__).parents[1]
    data = json.loads((root / "data/tournament_preview_examples.json").read_text())[0]
    data["branding"]["theme_key"] = "blast-open"
    profile = PreviewProfile.model_validate(data)
    assert profile.branding.theme_key == "blast-open" and not profile.approved
    with pytest.raises(PreviewUnavailable, match="profile_not_approved"):
        check_profile(profile, profile.verified_at)


@pytest.mark.parametrize("fault", ["duplicate", "unknown", "unapproved", "unsafe_logo"])
def test_invalid_theme_registry_is_rejected(fault):
    data = load_theme_registry().model_dump(mode="json")
    if fault == "duplicate":
        data["themes"].append(data["themes"][0])
    elif fault == "unknown":
        data["series"]["new-series"] = "missing"
    elif fault == "unapproved":
        data["themes"][0]["design_approved"] = False
    else:
        data["themes"][0]["logo_asset"] = "../secrets.png"
    with pytest.raises(ValidationError):
        ThemeRegistry.model_validate(data)


def test_all_bundled_theme_logos_are_sourced_valid_pngs():
    assets = Path(__file__).parents[1] / "cs2bot/assets/tournament-preview"
    for theme in load_theme_registry().themes:
        if theme.logo_asset:
            assert theme.logo_source_url
            with Image.open(assets / theme.logo_asset) as image:
                assert image.format == "PNG" and image.width * image.height <= 4_000_000
                assert image.convert("RGBA").getbbox()


@pytest.mark.parametrize("key", [theme.key for theme in load_theme_registry().themes])
def test_full_cover_uses_selected_palette_without_changing_passport(key):
    fixture = Path(__file__).parent / "fixtures/tournament_preview_demo.json"
    preview = TournamentPreview.model_validate_json(fixture.read_text())
    before = (preview.name, preview.start, preview.end, preview.prize_pool_usd, preview.locations)
    preview.branding = PreviewBranding(theme_key=key)
    image = Image.open(io.BytesIO(render_preview_cards(preview, demo=True)[0]))
    assert image.size == (1080, 1080) and image.format == "PNG"
    accent = resolve_branding(preview.branding).accent
    expected = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    assert image.getpixel((100, 136))[:3] == expected
    assert (preview.name, preview.start, preview.end, preview.prize_pool_usd, preview.locations) == before
