"""Regressions for final map consistency, Cyrillic names and streamed logos."""
import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest
import requests
from PIL import Image, ImageDraw

from cs2bot import media_cards as media
from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.final_wait import FinalWait
from cs2bot.match_sources.models import (
    MapResult, MatchNormalized, RadarBracketNode, TournamentPlacement,
    TournamentRadar, TournamentVRSImpact, confirmed_result_maps,
)


def final_match(count=3, **changes):
    wins = 2 if count == 3 else 3
    values = dict(source="liquipedia", match_id="review-final",
        tournament_name="IEM Cologne 2026", competition_key="IEM Cologne 2026",
        tournament_tier="s", is_lan=True,
        team1_name="Команда А", team2_name="Команда Б",
        score1=wins, score2=count - wins, best_of=3 if count == 3 else 5,
        date="2026-10-29T10:00:00Z", is_final=True, final_identity_confirmed=True,
        winner_prize_usd=100,
        maps=[MapResult(name=name, score1=13 if index < wins else 9,
                       score2=9 if index < wins else 13)
              for index, name in enumerate(("Mirage", "Nuke", "Ancient", "Inferno", "Train")[:count])])
    values.update(changes)
    return MatchNormalized(**values)


@pytest.mark.parametrize("count", [3, 4, 5])
def test_consistent_final_maps_are_ready_for_selection_and_both_formats(count):
    match = final_match(count)
    assert confirmed_result_maps(match) == match.maps
    assert media._confirmed_result_maps(match) == match.maps
    assert media.can_render_final_card(match)
    assert match_fetcher._has_complete_final_card(match)
    for card_format, size in [("square", (1080, 1080)), ("portrait", (1080, 1350))]:
        assert Image.open(io.BytesIO(media.render_final_card(match, card_format=card_format))).size == size


@pytest.mark.parametrize("change", [
    {"score1": 1, "score2": 2},
    {"maps": [MapResult(name=name, score1=13, score2=9) for name in ("Mirage", "Nuke", "Ancient")]},
    {"maps": [MapResult(name=name, score1=13, score2=13) for name in ("Mirage", "Nuke", "Ancient")]},
    {"maps": [MapResult(name=name, score1=13, score2=None) for name in ("Mirage", "Nuke", "Ancient")]},
    {"forfeit": True}, {"result_type": "walkover"}, {"best_of": 1},
])
def test_inconsistent_or_technical_final_uses_regular_result_without_maps(change, monkeypatch):
    match = final_match(**change)
    assert confirmed_result_maps(match) == []
    assert not media.can_render_final_card(match)
    assert not match_fetcher._has_complete_final_card(match)
    with pytest.raises(media.MediaCardError, match="confirmed final maps"):
        media.render_final_card(match)
    texts = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        texts.append(text)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    data = media.render_result_card(match, card_format="square")
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)
    assert "РЕЗУЛЬТАТ МАТЧА" in texts
    assert not {"MIRAGE", "NUKE", "ANCIENT"}.intersection(texts)


@pytest.mark.parametrize("consistent", [False, True])
def test_only_consistent_maps_release_primary_final_before_existing_wait_expires(monkeypatch, consistent):
    now = datetime(2026, 10, 29, 11, tzinfo=timezone.utc)
    final = final_match()
    if not consistent:
        final.maps[0].score2 = final.maps[0].score1
    primary = final.model_copy(update={"source": "pandascore", "final_candidate_hint": True})
    async def list_waits(**kwargs):
        return []
    async def observe(match, **kwargs):
        return FinalWait(match=match, first_seen_at=now - timedelta(minutes=10))
    monkeypatch.setattr(match_fetcher, "list_waits", list_waits)
    monkeypatch.setattr(match_fetcher, "observe_primary", observe)
    selected = asyncio.run(match_fetcher._wait_for_liquipedia_final_cards(
        [primary], [final], [final], now=now, dry_run=True))
    assert bool(selected) is consistent


@pytest.mark.parametrize("kind", ["final", "standings", "vrs", "radar", "initials"])
@pytest.mark.parametrize("card_format", ["square", "portrait"])
def test_cyrillic_names_and_initials_use_supported_glyphs(kind, card_format, monkeypatch):
    names = {"КОМАНДА А", "КОМАНДА Б", "КА", "КБ"}
    fonts = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        if text in names:
            fonts.append((text, kwargs["font"]))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    match = final_match()
    options = {"card_format": card_format}
    if kind == "final":
        media.render_final_card(match, **options)
    elif kind == "standings":
        media.render_tournament_standings_cards(match.tournament_name, [
            TournamentPlacement(placement=str(index + 1), team_name=name, prize_usd=100)
            for index, name in enumerate((match.team1_name, match.team2_name))], **options)
    elif kind == "vrs":
        media.render_tournament_vrs_cards(match.tournament_name, [
            TournamentVRSImpact(placement=str(index + 1), team_name=name, team_id=str(index),
                before_points=1000, after_points=1010, before_rank=20, after_rank=19,
                points_delta=10, rank_delta=1, source="Valve", before_version="before", after_version="after")
            for index, name in enumerate((match.team1_name, match.team2_name))], **options)
    elif kind == "radar":
        media.render_tournament_radar_cards(TournamentRadar(tournament_id="review", bracket_structure=[
            RadarBracketNode(match_id=str(index), team1_name=match.team1_name, team2_name=match.team2_name)
            for index in range(2)]), match.tournament_name, "Europe/Moscow", **options)
    else:
        canvas = Image.new("RGBA", media.static_card_size(card_format))
        media._draw_logo(canvas, ImageDraw.Draw(canvas), (100, 100), 80, match.team1_name, None, media.CYAN)
    assert fonts
    for text, font in fonts:
        missing = font.getmask(chr(0x10ffff))
        for char in text.replace(" ", ""):
            glyph = font.getmask(char)
            assert (glyph.size, bytes(glyph)) != (missing.size, bytes(missing)), (text, char)


def test_latin_data_keeps_its_existing_font(monkeypatch):
    canvas = Image.new("RGBA", (400, 100))
    draw = ImageDraw.Draw(canvas)
    assert media._fit_font(draw, "NAVI", 300, 40, 30).path == str(media.LATIN_BOLD_FONT)
    fonts = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        fonts.append(kwargs["font"].path)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    media._draw_text_block(draw, 200, 50, "NAVI", 300)
    assert fonts == [str(media.LATIN_BOLD_FONT)]


class LogoResponse:
    status_code = 200
    headers = {"Content-Type": "image/png"}

    def __init__(self, error=None):
        self.error, self.closed = error, False

    def iter_content(self, size):
        if self.error:
            yield b"partial body"
            raise self.error("stream interrupted")
        output = io.BytesIO()
        Image.new("RGBA", (20, 20), "red").save(output, "PNG")
        yield output.getvalue()

    def close(self):
        self.closed = True


@pytest.fixture
def offline_logo_cache(monkeypatch):
    media._logo_memory_cache.clear()
    monkeypatch.setattr(media, "read_cached_logo", lambda url: None)
    monkeypatch.setattr(media, "write_cached_logo", lambda *args: None)
    yield
    media._logo_memory_cache.clear()


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.ReadTimeout, requests.exceptions.ChunkedEncodingError])
def test_stream_failure_tries_original_after_thumbnail_and_closes_responses(error, monkeypatch, offline_logo_cache):
    responses = [LogoResponse(error), LogoResponse()]
    requested = []
    def get(url, **kwargs):
        requested.append(url)
        return responses[len(requested) - 1]
    monkeypatch.setattr(media.requests, "get", get)
    assert media.fetch_team_logo("https://cdn.pandascore.co/images/team/image/1/logo.png") is not None
    assert len(requested) == 2 and "thumb_logo.png" in requested[0]
    assert all(response.closed for response in responses)


def test_stream_failure_uses_existing_fallback_logo(monkeypatch, offline_logo_cache):
    responses = [LogoResponse(requests.ConnectionError), LogoResponse()]
    requested = []
    def get(url, **kwargs):
        requested.append(url)
        return responses[len(requested) - 1]
    monkeypatch.setattr(media.requests, "get", get)
    canvas = Image.new("RGBA", (200, 200))
    media._draw_logo(canvas, ImageDraw.Draw(canvas), (100, 100), 80, "NAVI",
        "https://cdn.pandascore.co/images/team/image/1/250px_primary.png", media.CYAN,
        "https://cdn.pandascore.co/images/team/image/1/250px_fallback.png")
    assert canvas.getpixel((100, 100)) == (255, 0, 0, 255)
    assert len(requested) == 2 and all(response.closed for response in responses)


def test_exhausted_stream_failures_are_media_errors_and_do_not_block_card(monkeypatch, offline_logo_cache):
    responses = []
    def get(*args, **kwargs):
        response = LogoResponse(requests.exceptions.ChunkedEncodingError)
        responses.append(response)
        return response
    monkeypatch.setattr(media.requests, "get", get)
    url = "https://cdn.pandascore.co/images/team/image/1/250px_primary.png"
    with pytest.raises(media.MediaCardError, match="response failed") as failure:
        media.fetch_team_logo(url)
    assert isinstance(failure.value.__cause__, requests.exceptions.ChunkedEncodingError)
    data = media.render_result_card(final_match(team1_logo_url=url), card_format="square")
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)
    assert responses and all(response.closed for response in responses)
