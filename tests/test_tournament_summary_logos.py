"""Regression: source event logos survive the tournament-summary rendering path."""
import io

import pytest
from PIL import Image

from cs2bot import media_cards
from cs2bot.match_sources.models import TournamentPlacement, TournamentVRSImpact
from cs2bot.tournament_branding import branding_for_tournament


@pytest.mark.parametrize("kind", ["standings", "vrs"])
@pytest.mark.parametrize("row_count", [2, 4, 10])
def test_summary_source_logo_is_visible_on_every_page(monkeypatch, kind, row_count):
    logo_url = "https://example.com/tournament.png"
    def fetch(url):
        assert url == logo_url
        return Image.new("RGBA", (220, 100), (255, 0, 255, 255))
    monkeypatch.setattr(media_cards, "fetch_team_logo", fetch)
    placements = [TournamentPlacement(placement=str(i + 1),
        team_name="Long Team Name " * 3, prize_usd=1000) for i in range(row_count)]
    branding = branding_for_tournament("Thunderpick World Championship 2026")
    assert branding is not None
    if kind == "standings":
        pages = media_cards.render_tournament_standings_cards(
            "Thunderpick World Championship 2026", placements,
            branding=branding, tournament_logo_url=logo_url)
    else:
        impacts = [TournamentVRSImpact(placement=p.placement, team_name=p.team_name,
            team_id=str(i), before_points=1000, after_points=1010,
            before_rank=i+2, after_rank=i+1, points_delta=10, rank_delta=1,
            source="Valve VRS live/global", before_version="2026-10-05",
            after_version="2026-11-02") for i,p in enumerate(placements)]
        pages = media_cards.render_tournament_vrs_cards(
            "Thunderpick World Championship 2026", impacts,
            branding=branding, tournament_logo_url=logo_url)
    for page in pages:
        image = Image.open(io.BytesIO(page))
        assert image.size == (1080, 1350)
        assert image.getpixel((890, 223))[:3] == (255, 0, 255)


def test_unavailable_summary_logo_preserves_text_fallback(monkeypatch):
    def unavailable(url):
        raise media_cards.MediaCardError("unavailable")
    monkeypatch.setattr(media_cards, "fetch_team_logo", unavailable)
    placements = [TournamentPlacement(placement=str(i+1), team_name=f"Team {i}",
        prize_usd=1000) for i in range(2)]
    cards = media_cards.render_tournament_standings_cards(
        "Thunderpick World Championship 2026", placements,
        branding=branding_for_tournament("Thunderpick World Championship 2026"),
        tournament_logo_url="https://example.com/unavailable.png")
    assert Image.open(io.BytesIO(cards[0])).size == (1080, 1350)
