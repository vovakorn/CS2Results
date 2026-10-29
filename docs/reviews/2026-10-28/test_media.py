"""Offline review evidence for b363ac0; tests demonstrate existing defects."""
import io
import os
import sys
if os.getenv('CS2_REVIEW_SOURCE'):
    sys.path.insert(0, os.environ['CS2_REVIEW_SOURCE'])
from PIL import Image, ImageDraw
import pytest
import requests
from cs2bot import media_cards as media
from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.models import MapResult, MatchNormalized, TournamentPlacement


def final_match(**updates):
    data = dict(source='liquipedia', match_id='audit-final', tournament_name='Audit event',
                team1_name='Команда А', team2_name='Команда Б', score1=2, score2=1,
                best_of=3, is_final=True, final_identity_confirmed=True,
                winner_prize_usd=100,
                maps=[MapResult(name=name,score1=13,score2=5) for name in ('Mirage','Nuke','Ancient')])
    data.update(updates)
    return MatchNormalized(**data)


def test_contradictory_final_maps_are_admitted_and_rendered():
    match = final_match()
    assert media._confirmed_result_maps(match) == []
    assert match_fetcher._has_complete_final_card(match) is True
    assert media.can_render_final_card(match) is True
    image = media.render_final_card(match, card_format='square')
    assert Image.open(io.BytesIO(image)).size == (1080,1080)


def test_tied_final_map_is_admitted():
    match = final_match(maps=[MapResult(name='Mirage',score1=13,score2=13),
                             MapResult(name='Nuke',score1=13,score2=5),
                             MapResult(name='Ancient',score1=5,score2=13)])
    assert media._confirmed_result_maps(match) == []
    assert media.can_render_final_card(match) is True


def test_cyrillic_standings_names_select_missing_glyph_font(monkeypatch):
    calls=[]
    original=ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        if text in ('КОМАНДА А', 'КОМАНДА Б'):
            calls.append((text, kwargs['font']))
        return original(draw,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    media.render_tournament_standings_cards('Audit event',[
        TournamentPlacement(placement='1',team_name='Команда А',prize_usd=100),
        TournamentPlacement(placement='2',team_name='Команда Б',prize_usd=0)])
    assert len(calls)==2
    for _,font in calls:
        missing=font.getmask(chr(0x10ffff)); russian=font.getmask('А')
        assert (missing.size,bytes(missing))==(russian.size,bytes(russian))


def test_streaming_logo_failure_escapes_fallback_error(monkeypatch):
    class Response:
        status_code=200
        headers={'Content-Type':'image/png'}
        def iter_content(self,chunk_size):
            raise requests.ConnectionError('body interrupted')
            yield b''
        def close(self): pass
    monkeypatch.setattr(media,'_cached_logo',lambda url:None)
    monkeypatch.setattr(media.requests,'get',lambda *args,**kwargs:Response())
    with pytest.raises(requests.ConnectionError):
        media.fetch_team_logo('https://cdn.pandascore.co/images/team/image/1/test.png')
