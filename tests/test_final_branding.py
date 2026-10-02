import io

from PIL import Image
import pytest

from cs2bot import media_cards
from cs2bot.match_sources.models import MapResult, MatchNormalized, TournamentPlacement
from cs2bot.tournament_preview import PreviewBranding


def final(count=4):
    return MatchNormalized(source="liquipedia", match_id="final-brand-test",
        tournament_name="PGL Masters Bucharest 2026", tournament_parent="PGL/2026/Masters",
        team1_name="Team Spirit International", team2_name="FUT Esports",
        score1=3, score2=count - 3, is_final=True, winner_prize_usd=300000,
        maps=[MapResult(name=f"Map {index + 1}", score1=13, score2=8)
              for index in range(count)])


@pytest.mark.parametrize("count", [3, 4, 5])
def test_red_brand_keeps_square_card_and_both_red_header_segments(count):
    image = Image.open(io.BytesIO(media_cards.render_final_card(final(count),
        branding=PreviewBranding(accent="#ED3D55"))))
    assert image.format == "PNG"
    assert image.size == (1080, 1080)
    for x in (80, 1000):
        red, green, blue = image.getpixel((x, 90))
        assert red > green * 2 and red > blue * 2


def test_brand_uses_exact_event_profile_and_unknown_event_keeps_gold():
    from cs2bot.match_sources.config import TOURNAMENT_PREVIEW_PROFILES_PATH
    from cs2bot.tournament_preview import load_profiles

    profile = load_profiles(TOURNAMENT_PREVIEW_PROFILES_PATH)[0]
    match = final().model_copy(update={"tournament_parent": profile.liquipedia_page})
    assert media_cards._final_event_branding(match) == profile.branding
    assert media_cards._final_event_branding(final()) is None


def test_missing_logo_keeps_brand_and_render_usable():
    brand = PreviewBranding(accent="#ED3D55", logo_asset="missing-logo.png",
                             logo_source_url="https://example.com/logo.png")
    image = Image.open(io.BytesIO(media_cards.render_final_card(final(), branding=brand)))
    assert image.size == (1080, 1080)


@pytest.mark.parametrize("team_count", [2, 4, 10, 16])
def test_standings_keep_every_team_and_brand_on_every_page(team_count, monkeypatch):
    placements = [TournamentPlacement(placement=str(i + 1), team_name=f"Team {i + 1}",
                                      prize_usd=(team_count - i) * 10000)
                  for i in range(team_count)]
    drawn_names = []
    original = media_cards._draw_text_block

    def capture(draw, x, y, text, *args, **kwargs):
        if text.startswith("TEAM "):
            drawn_names.append(text)
        return original(draw, x, y, text, *args, **kwargs)

    monkeypatch.setattr(media_cards, "_draw_text_block", capture)
    cards = media_cards.render_tournament_standings_cards("PGL Masters Bucharest 2026", placements,
                                                          branding=PreviewBranding(accent="#ED3D55"))
    assert len(cards) == (team_count + 7) // 8
    assert drawn_names == [item.team_name.upper() for item in placements]
    for data in cards:
        image = Image.open(io.BytesIO(data))
        assert image.format == "PNG" and image.size == (1080, 1080)
        for x in (80, 1000):
            red, green, blue = image.getpixel((x, 90))
            assert red > green * 2 and red > blue * 2
