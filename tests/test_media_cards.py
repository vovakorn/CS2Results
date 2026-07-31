import io

import pytest
from PIL import Image

from cs2bot import media_cards
from cs2bot.match_sources.models import MatchNormalized, UpcomingMatchNormalized


def _result():
    return MatchNormalized(
        source="pandascore",
        match_id="result-1",
        tournament_name="BLAST Bounty — 2026 Season 2 Finals",
        team1_name="3DMAX",
        team2_name="MOUZ",
        score1=1,
        score2=2,
        best_of=3,
        date="2026-07-30T18:20:00Z",
    )


def _upcoming(match_id="upcoming-1"):
    return UpcomingMatchNormalized(
        match_id=match_id,
        tournament_name="BLAST Bounty — 2026 Season 2 Finals",
        competition_key="BLAST Bounty 2026",
        team1_name="Liquid",
        team2_name="Spirit",
        scheduled_at="2026-07-31T15:30:00Z",
        best_of=3,
        is_featured=True,
    )


def test_result_card_is_valid_square_png_without_logos():
    data = media_cards.render_result_card(_result())

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.RESULT_CARD_SIZE


def test_schedule_card_is_valid_portrait_png():
    data = media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.SCHEDULE_CARD_SIZE


def test_schedule_card_rejects_more_than_six_matches():
    with pytest.raises(media_cards.MediaCardError, match="six"):
        media_cards.render_schedule_card(
            [_upcoming(str(index)) for index in range(7)],
            media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
            "Europe/Moscow",
        )


def test_logo_download_rejects_non_pandascore_host_without_request(monkeypatch):
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(media_cards.requests, "get", fake_get)

    assert media_cards.fetch_team_logo("https://attacker.example/logo.png") is None
    assert called is False


def test_logo_download_accepts_current_pandascore_cdn(monkeypatch):
    output = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(output, "PNG")
    raw = output.getvalue()
    requested = []

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": str(len(raw))}

        def iter_content(self, size):
            yield raw

        def close(self):
            pass

    def fake_get(url, **kwargs):
        requested.append(url)
        return FakeResponse()

    monkeypatch.setattr(media_cards.requests, "get", fake_get)

    logo = media_cards.fetch_team_logo(
        "https://cdn-api.pandascore.co/images/team/image/3210/250px_g2.png"
    )

    assert logo is not None
    assert requested == [
        "https://cdn-api.pandascore.co/images/team/image/3210/250px_g2.png"
    ]


def test_logo_download_rejects_redirect(monkeypatch):
    class FakeResponse:
        status_code = 302
        headers = {"Content-Type": "image/png"}

        def close(self):
            pass

    monkeypatch.setattr(media_cards.requests, "get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(media_cards.MediaCardError, match="HTTP 302"):
        media_cards.fetch_team_logo(
            "https://cdn.pandascore.co/images/team/image/1/logo.png"
        )


def test_logo_download_accepts_small_png(monkeypatch):
    output = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(output, "PNG")
    raw = output.getvalue()

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": str(len(raw))}

        def iter_content(self, size):
            yield raw

        def close(self):
            pass

    monkeypatch.setattr(media_cards.requests, "get", lambda *args, **kwargs: FakeResponse())

    logo = media_cards.fetch_team_logo(
        "https://cdn.pandascore.co/images/team/image/1/logo.png"
    )

    assert logo is not None
    assert logo.size == (20, 20)


def test_logo_download_prefers_official_thumbnail(monkeypatch):
    output = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(output, "PNG")
    raw = output.getvalue()
    requested = []

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/octet-stream"}

        def iter_content(self, size):
            yield raw

        def close(self):
            pass

    def fake_get(url, **kwargs):
        requested.append(url)
        return FakeResponse()

    monkeypatch.setattr(media_cards.requests, "get", fake_get)

    logo = media_cards.fetch_team_logo(
        "https://cdn.pandascore.co/images/team/image/1/logo.png"
    )

    assert logo is not None
    assert requested == [
        "https://cdn.pandascore.co/images/team/image/1/thumb_logo.png"
    ]


def test_schedule_uses_full_tournament_name_not_competition_key(monkeypatch):
    drawn = []
    original = media_cards._centered_text

    def capture(draw, center_x, y, text, font, fill):
        drawn.append(text)
        return original(draw, center_x, y, text, font, fill)

    monkeypatch.setattr(media_cards, "_centered_text", capture)

    media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert "BLAST BOUNTY — 2026 SEASON 2 FINALS" in drawn
    assert "BLAST BOUNTY 2026" not in drawn
