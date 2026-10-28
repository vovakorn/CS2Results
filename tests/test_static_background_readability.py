"""Static templates quiet stripe motifs while retaining the original atlas API."""
from pathlib import Path

from PIL import Image, ImageChops
import pytest

from cs2bot import media_cards as media, tournament_preview_cards as previews
from cs2bot.tournament_branding import branding_for_tournament
from cs2bot.tournament_preview import TournamentPreview
from cs2bot.tournament_theme_patterns import preview_background


@pytest.mark.parametrize("motif", ["diagonal", "ember", "metal", "grid"])
@pytest.mark.parametrize("card_format", ["square", "portrait"])
def test_static_fixture_and_preview_use_quiet_stripes(motif, card_format):
    brand = branding_for_tournament("PGL Masters Bucharest").model_copy(update={"motif": motif})
    size = media.static_card_size(card_format)
    opacity = .5 if motif in ("diagonal", "ember", "metal") else 1.0
    expected = preview_background(size, brand, motif_opacity=opacity).convert("RGB")
    fixture = media._fixture_canvas(brand, size).convert("RGB")
    assert fixture.tobytes() == expected.tobytes()

    preview = TournamentPreview.model_validate_json(
        (Path(__file__).parent / "fixtures/tournament_preview_demo.json").read_text())
    preview = preview.model_copy(update={"branding": brand})
    cover, _ = previews._canvas(preview, "ПЕРЕД СТАРТОМ", card_format)
    # This edge is outside the frame, header and watermark, so it measures
    # the background produced by the real announcement canvas callsite.
    edge = (0, 0, 20, size[1])
    assert cover.convert("RGB").crop(edge).tobytes() == expected.crop(edge).tobytes()

    original = preview_background(size, brand).convert("RGB")
    difference = ImageChops.difference(fixture, original).getbbox()
    assert (difference is not None) == (opacity == .5)


def test_default_atlas_background_remains_original_and_full_opacity():
    brand = branding_for_tournament("PGL Masters Bucharest")
    original = Path(__file__).parents[1] / "docs/design/tournament-atlas-2026-10-03/samples/pgl.png"
    options = {"frame": False, "header": False}
    default = preview_background((600, 480), brand, **options).convert("RGB")
    explicit = preview_background((600, 480), brand, motif_opacity=1.0, **options).convert("RGB")
    assert default.tobytes() == explicit.tobytes()
    with Image.open(original) as archived:
        for edge in ((0, 0, 20, 480), (580, 0, 600, 480)):
            assert default.crop(edge).tobytes() == archived.convert("RGB").crop(edge).tobytes()
