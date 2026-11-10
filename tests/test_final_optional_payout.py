import io

import pytest
from PIL import Image

from cs2bot import media_cards
from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.models import MapResult
from test_beijing_final_recovery import beijing_pair


@pytest.mark.parametrize("count", [2, 3, 4, 5])
@pytest.mark.parametrize("card_format,size", [("portrait", (1080, 1350)), ("square", (1080, 1080))])
def test_confirmed_final_renders_played_maps_without_money(monkeypatch, beijing_pair, count, card_format, size):
    _, raw = beijing_pair
    wins = 2 if count <= 3 else 3
    raw.score1, raw.score2 = wins, count - wins
    raw.best_of = 3 if count <= 3 else 5
    raw.maps = [MapResult(name=name, score1=13 if i < wins else 6, score2=6 if i < wins else 13)
                for i, name in enumerate(["Mirage", "Dust II", "Nuke", "Ancient", "Inferno"][:count])]
    raw.winner_prize_usd = None
    for placement in raw.tournament_placements:
        placement.prize_usd = None
    final = match_fetcher.confirm_liquipedia_final_candidates([raw])[0][0]
    assert media_cards.can_render_final_card(final)
    monkeypatch.setattr(media_cards, "_format_usd", lambda value: pytest.fail("invented a payout"))
    image = Image.open(io.BytesIO(media_cards.render_final_card(final, card_format=card_format)))
    assert image.size == size


def test_missing_money_does_not_admit_an_unconfirmed_or_inconsistent_final(beijing_pair):
    _, raw = beijing_pair
    raw.winner_prize_usd = None
    assert not media_cards.can_render_final_card(raw)
    final = match_fetcher.confirm_liquipedia_final_candidates([raw])[0][0]
    final.maps = final.maps[:-1]
    assert not media_cards.can_render_final_card(final)
