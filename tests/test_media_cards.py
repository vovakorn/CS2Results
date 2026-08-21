import io

import pytest
from PIL import Image

from cs2bot import media_cards
from cs2bot.match_sources.models import (
    MapResult,
    MatchNormalized,
    RadarBracketMatch,
    RadarStandingTeam,
    TournamentRadar,
    UpcomingMatchNormalized,
)


@pytest.fixture(autouse=True)
def clear_logo_memory_cache():
    media_cards._logo_memory_cache.clear()
    yield
    media_cards._logo_memory_cache.clear()


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


def test_final_card_requires_confirmed_final_data_and_is_square_png():
    match = _result().model_copy(update={
        "is_final": True,
        "winner_prize_usd": 500_000,
        "maps": [
            MapResult(name="Mirage", score1=13, score2=9),
            MapResult(name="Nuke", score1=11, score2=13),
            MapResult(name="Ancient", score1=13, score2=10),
        ],
    })
    image = Image.open(io.BytesIO(media_cards.render_final_card(match)))
    assert image.format == "PNG"
    assert image.size == media_cards.RESULT_CARD_SIZE
    assert media_cards.can_render_final_card(match)
    assert not media_cards.can_render_final_card(_result())


def test_result_card_channel_logo_is_centered_at_top(monkeypatch):
    drawn = []

    def capture(canvas, draw, center, diameter):
        drawn.append((center, diameter))

    monkeypatch.setattr(media_cards, "_draw_channel_logo", capture)

    media_cards.render_result_card(_result())

    assert drawn == [((media_cards.RESULT_CARD_SIZE[0] // 2, 98), 100)]


def test_result_card_aligns_team_logos_and_names_to_outer_edges(monkeypatch):
    names = []
    logos = []
    original = media_cards._aligned_text

    def capture_text(draw, edge_x, y, text, font, fill, alignment):
        names.append((edge_x, y, text, alignment))
        return original(draw, edge_x, y, text, font, fill, alignment)

    def capture_logo(canvas, draw, center, diameter, *args):
        logos.append((center, diameter))

    monkeypatch.setattr(media_cards, "_aligned_text", capture_text)
    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    match = _result().model_copy(
        update={
            "team1_name": "Natus Vincere Junior",
            "team2_name": "Gaimin Gladiators Academy",
        }
    )

    media_cards.render_result_card(match)

    assert [alignment for _, _, _, alignment in names] == ["left", "right"]
    assert logos[0][0][0] - logos[0][1] // 2 == names[0][0]
    assert logos[1][0][0] + logos[1][1] // 2 == names[1][0]
    assert names[0][1] - (logos[0][0][1] + logos[0][1] // 2) >= 24
    assert names[1][1] - (logos[1][0][1] + logos[1][1] // 2) >= 24


def test_daily_results_card_supports_ten_matches():
    matches = [
        _result().model_copy(
            update={
                "match_id": str(index),
                "team1_name": f"Long Left Team {index}",
                "team2_name": f"Long Right Team {index}",
            }
        )
        for index in range(10)
    ]

    data = media_cards.render_results_card(
        matches,
        media_cards.datetime.fromisoformat("2026-08-01T23:00:00+03:00"),
    )

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.RESULT_CARD_SIZE


def test_daily_results_card_rejects_more_than_ten_matches():
    with pytest.raises(media_cards.MediaCardError, match="ten"):
        media_cards.render_results_card(
            [_result().model_copy(update={"match_id": str(index)}) for index in range(11)],
            media_cards.datetime.fromisoformat("2026-08-01T23:00:00+03:00"),
        )


def test_schedule_card_is_valid_square_png():
    data = media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.SCHEDULE_CARD_SIZE


@pytest.mark.parametrize("variant", ["standings", "bracket", "next_match"])
def test_tournament_radar_card_variants_are_valid_square_png(variant):
    radar = TournamentRadar(
        tournament_id="3",
        standings=["1. NAVI", "2. FaZe", "3. Spirit", "4. Vitality"],
        standing_teams=[
            RadarStandingTeam(rank=1, name="NAVI"),
            RadarStandingTeam(rank=2, name="FaZe"),
            RadarStandingTeam(rank=3, name="Spirit"),
            RadarStandingTeam(rank=4, name="Vitality"),
        ],
        bracket_matches=[
            RadarBracketMatch(match_id="semi-1", round_name="Semifinal", team1_name="NAVI", team2_name="FaZe"),
            RadarBracketMatch(match_id="semi-2", round_name="Semifinal", team1_name="Spirit", team2_name="Vitality"),
        ],
        next_matches=[_upcoming()],
        roster_team_count=16,
        bracket_match_count=12,
    )

    data = media_cards.render_tournament_radar_card(
        radar, "IEM Cologne 2026", "Europe/Moscow", variant
    )

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.SCHEDULE_CARD_SIZE


def test_tournament_radar_card_rejects_unknown_variant():
    with pytest.raises(media_cards.MediaCardError, match="variant"):
        media_cards.render_tournament_radar_card(
            TournamentRadar(tournament_id="3"), "IEM Cologne 2026", "Europe/Moscow", "unknown"
        )


def test_schedule_card_supports_ten_matches():
    data = media_cards.render_schedule_card(
        [_upcoming(str(index)) for index in range(10)],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    image = Image.open(io.BytesIO(data))
    assert image.size == (1080, 1080)


def test_schedule_card_rejects_more_than_ten_matches():
    with pytest.raises(media_cards.MediaCardError, match="ten"):
        media_cards.render_schedule_card(
            [_upcoming(str(index)) for index in range(11)],
            media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
            "Europe/Moscow",
        )


def test_schedule_album_balances_sixteen_matches_into_two_pages():
    matches = [
        _upcoming(str(index)).model_copy(
            update={"scheduled_at": f"2026-07-31T{index:02d}:00:00Z"}
        )
        for index in range(16)
    ]

    pages = media_cards.paginate_schedule_matches(list(reversed(matches)))

    assert [len(page) for page in pages] == [8, 8]
    assert [match.match_id for match in pages[0]] == [str(index) for index in range(8)]
    assert [match.match_id for match in pages[1]] == [str(index) for index in range(8, 16)]


def test_schedule_album_balances_odd_match_count():
    pages = media_cards.paginate_schedule_matches(
        [_upcoming(str(index)) for index in range(15)]
    )

    assert [len(page) for page in pages] == [8, 7]


def test_schedule_album_renders_two_square_pngs_for_sixteen_matches():
    cards = media_cards.render_schedule_cards(
        [_upcoming(str(index)) for index in range(16)],
        media_cards.datetime.fromisoformat("2026-08-12T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert len(cards) == 2
    for data in cards:
        image = Image.open(io.BytesIO(data))
        assert image.format == "PNG"
        assert image.size == media_cards.SCHEDULE_CARD_SIZE


def test_schedule_album_marks_page_number_in_date_line(monkeypatch):
    centered = []
    original = media_cards._centered_text

    def capture(draw, center_x, y, text, font, fill):
        centered.append((y, text))
        return original(draw, center_x, y, text, font, fill)

    monkeypatch.setattr(media_cards, "_centered_text", capture)

    media_cards.render_schedule_card(
        [_upcoming(str(index)) for index in range(8)],
        media_cards.datetime.fromisoformat("2026-08-12T10:00:00+03:00"),
        "Europe/Moscow",
        page_number=2,
        page_count=2,
    )

    assert (315, "12 АВГУСТА · 2/2") in centered


def test_schedule_album_rejects_more_than_twenty_matches():
    with pytest.raises(media_cards.MediaCardError, match="twenty"):
        media_cards.render_schedule_cards(
            [_upcoming(str(index)) for index in range(21)],
            media_cards.datetime.fromisoformat("2026-08-12T10:00:00+03:00"),
            "Europe/Moscow",
        )


def test_header_accent_lines_are_mirrored_to_the_outer_edges():
    cyan, amber = media_cards._header_accent_segments(1080)

    assert cyan == (55, 485)
    assert amber == (595, 1025)
    assert cyan[1] - cyan[0] == amber[1] - amber[0]


def test_dark_logo_gets_light_contrast_plate():
    logo = Image.new("RGBA", (100, 100), (8, 10, 14, 255))

    assert media_cards._logo_plate_fill(logo) == media_cards.LOGO_PLATE_LIGHT


def test_white_logo_gets_dark_contrast_plate():
    logo = Image.new("RGBA", (100, 100), (248, 248, 248, 255))

    assert media_cards._logo_plate_fill(logo) == media_cards.LOGO_PLATE_DARK


def test_transparent_padding_does_not_change_logo_plate_choice():
    logo = Image.new("RGBA", (100, 100), (255, 255, 255, 0))
    for x in range(35, 65):
        for y in range(35, 65):
            logo.putpixel((x, y), (4, 6, 9, 255))

    assert media_cards._logo_plate_fill(logo) == media_cards.LOGO_PLATE_LIGHT


def test_mixed_black_and_red_logo_gets_light_plate():
    logo = Image.new("RGBA", (100, 100), (12, 12, 14, 255))
    for x in range(60, 100):
        for y in range(100):
            logo.putpixel((x, y), (218, 28, 44, 255))

    assert media_cards._logo_plate_fill(logo) == media_cards.LOGO_PLATE_LIGHT


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


def test_logo_download_uses_persistent_cache_before_cdn(monkeypatch):
    output = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(output, "PNG")
    raw = output.getvalue()
    url = "https://cdn-api.pandascore.co/images/team/image/1/250px_team.png"

    media_cards._logo_memory_cache.clear()
    monkeypatch.setattr(media_cards, "read_cached_logo", lambda value: raw if value == url else None)
    monkeypatch.setattr(
        media_cards.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("CDN must not be requested when cache contains logo"),
    )

    logo = media_cards.fetch_team_logo(url)

    assert logo is not None
    assert logo.size == (20, 20)


def test_logo_cache_failure_does_not_prevent_cdn_fetch(monkeypatch):
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

    monkeypatch.setattr(
        media_cards,
        "read_cached_logo",
        lambda value: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )
    monkeypatch.setattr(
        media_cards,
        "write_cached_logo",
        lambda value, data: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )
    monkeypatch.setattr(media_cards.requests, "get", lambda *args, **kwargs: FakeResponse())

    logo = media_cards.fetch_team_logo(
        "https://cdn-api.pandascore.co/images/team/image/1/250px_team.png"
    )

    assert logo is not None


def test_logo_download_uses_cdn_timeout_that_allows_cold_responses(monkeypatch):
    output = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(output, "PNG")
    raw = output.getvalue()
    requested_timeouts = []

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": str(len(raw))}

        def iter_content(self, size):
            yield raw

        def close(self):
            pass

    def fake_get(url, **kwargs):
        requested_timeouts.append(kwargs["timeout"])
        return FakeResponse()

    monkeypatch.setattr(media_cards.requests, "get", fake_get)

    logo = media_cards.fetch_team_logo(
        "https://cdn-api.pandascore.co/images/team/image/1/250px_team.png"
    )

    assert logo is not None
    assert requested_timeouts == [media_cards.LOGO_DOWNLOAD_TIMEOUT_SECONDS]
    assert media_cards.LOGO_DOWNLOAD_TIMEOUT_SECONDS >= 5.0


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
    assert all("ВРЕМЯ МСК" not in text for text in drawn)


def test_schedule_header_is_centered_on_canvas(monkeypatch):
    drawn = []
    original = media_cards._centered_text

    def capture(draw, center_x, y, text, font, fill):
        drawn.append((center_x, text))
        return original(draw, center_x, y, text, font, fill)

    monkeypatch.setattr(media_cards, "_centered_text", capture)

    media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    canvas_center = media_cards.SCHEDULE_CARD_SIZE[0] // 2
    assert (canvas_center, "МАТЧИ CS2 СЕГОДНЯ") in drawn
    assert (canvas_center, "BLAST BOUNTY — 2026 SEASON 2 FINALS") in drawn
    assert (canvas_center, "31 ИЮЛЯ") in drawn


@pytest.mark.parametrize("match_count", [1, 4, 10])
def test_schedule_shows_shared_tournament_header_for_every_layout(monkeypatch, match_count):
    drawn = []
    original = media_cards._centered_text

    def capture(draw, center_x, y, text, font, fill):
        drawn.append((center_x, y, text))
        return original(draw, center_x, y, text, font, fill)

    monkeypatch.setattr(media_cards, "_centered_text", capture)
    media_cards.render_schedule_card(
        [_upcoming(str(index)) for index in range(match_count)],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert (540, 254, "BLAST BOUNTY — 2026 SEASON 2 FINALS") in drawn


def test_schedule_uses_mixed_tournament_header_without_a_logo(monkeypatch):
    logos = []
    drawn = []
    original = media_cards._centered_text

    def capture_text(draw, center_x, y, text, font, fill):
        drawn.append((center_x, y, text))
        return original(draw, center_x, y, text, font, fill)

    def capture_logo(*args):
        logos.append(args)

    monkeypatch.setattr(media_cards, "_centered_text", capture_text)
    monkeypatch.setattr(media_cards, "_draw_tournament_logo", capture_logo)
    media_cards.render_schedule_card(
        [
            _upcoming("one"),
            _upcoming("two").model_copy(
                update={
                    "competition_key": "IEM Cologne 2026",
                    "tournament_name": "IEM — IEM Cologne 2026 — Playoffs",
                }
            ),
        ],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert (540, 254, "ТУРНИРЫ ДНЯ") in drawn
    assert logos == []


def test_schedule_draws_official_tournament_logo_in_header(monkeypatch):
    logos = []

    def capture_logo(canvas, draw, center, diameter, logo_url):
        logos.append((center, diameter, logo_url))

    match = _upcoming().model_copy(
        update={"tournament_logo_url": "https://cdn.pandascore.co/images/serie/image/2/iem.png"}
    )
    monkeypatch.setattr(media_cards, "_draw_tournament_logo", capture_logo)
    media_cards.render_schedule_card(
        [match], media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"), "Europe/Moscow"
    )

    assert len(logos) == 1
    assert logos[0][1:] == (54, "https://cdn.pandascore.co/images/serie/image/2/iem.png")


def test_schedule_channel_logo_is_centered_at_top(monkeypatch):
    drawn = []

    def capture(canvas, draw, center, diameter):
        drawn.append((center, diameter))

    monkeypatch.setattr(media_cards, "_draw_channel_logo", capture)

    media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert drawn == [((media_cards.SCHEDULE_CARD_SIZE[0] // 2, 98), 100)]


def test_schedule_team_names_are_aligned_to_outer_edges(monkeypatch):
    drawn = []
    original = media_cards._aligned_text

    def capture(draw, edge_x, y, text, font, fill, alignment):
        drawn.append((edge_x, text, alignment))
        return original(draw, edge_x, y, text, font, fill, alignment)

    monkeypatch.setattr(media_cards, "_aligned_text", capture)
    match = _upcoming().model_copy(
        update={
            "team1_name": "Natus Vincere Junior",
            "team2_name": "Gaimin Gladiators Academy",
        }
    )

    media_cards.render_schedule_card(
        [match],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert (108, "NATUS VINCERE JUNIOR", "left") in drawn
    assert (972, "GAIMIN GLADIATORS ACADEMY", "right") in drawn


def test_ten_match_schedule_centers_team_names_under_larger_logos(monkeypatch):
    drawn = []
    logos = []
    original = media_cards._centered_text

    def capture(draw, center_x, y, text, font, fill):
        if text.startswith("LONG "):
            drawn.append((center_x, y, text))
        return original(draw, center_x, y, text, font, fill)

    def capture_logo(canvas, draw, center, diameter, *args):
        logos.append((center, diameter))

    monkeypatch.setattr(media_cards, "_centered_text", capture)
    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    matches = [
        _upcoming(str(index)).model_copy(
            update={
                "team1_name": f"Long Left Team {index}",
                "team2_name": f"Long Right Team {index}",
            }
        )
        for index in range(10)
    ]

    media_cards.render_schedule_card(
        matches,
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert len(drawn) == 20
    for index in range(10):
        left_logo, right_logo = logos[index * 2 : index * 2 + 2]
        left_name, right_name = drawn[index * 2 : index * 2 + 2]
        assert left_logo[0][0] == left_name[0]
        assert right_logo[0][0] == right_name[0]
        assert left_name[1] > left_logo[0][1]
        assert right_name[1] > right_logo[0][1]
        assert left_logo[1] >= 48
        assert right_logo[1] >= 48


@pytest.mark.parametrize("match_count", [4, 8, 10])
def test_compact_schedule_uses_equal_sized_blocks(monkeypatch, match_count):
    boxes = []

    def capture(canvas, draw, match, box, display_timezone):
        boxes.append(box)

    monkeypatch.setattr(media_cards, "_draw_compact_schedule_match", capture)

    media_cards.render_schedule_card(
        [_upcoming(str(index)) for index in range(match_count)],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert len(boxes) == match_count
    assert {(x1 - x0, y1 - y0) for x0, y0, x1, y1 in boxes} == {(468, boxes[0][3] - boxes[0][1])}


def test_ten_match_schedule_leaves_margin_above_compact_logos(monkeypatch):
    logos = []
    boxes = []
    original = media_cards._draw_compact_schedule_match

    def capture_logo(canvas, draw, center, diameter, *args):
        logos.append((center, diameter))

    def capture_box(canvas, draw, match, box, display_timezone):
        boxes.append(box)
        return original(canvas, draw, match, box, display_timezone)

    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    monkeypatch.setattr(media_cards, "_draw_compact_schedule_match", capture_box)

    media_cards.render_schedule_card(
        [_upcoming(str(index)) for index in range(10)],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    for index, box in enumerate(boxes):
        y0 = box[1]
        for center, diameter in logos[index * 2 : index * 2 + 2]:
            assert center[1] - diameter // 2 - y0 >= 20


def test_schedule_uses_fallback_logo_when_primary_variant_fails(monkeypatch):
    match = _upcoming()
    match = match.model_copy(
        update={
            "team1_logo_url": "https://cdn-api.pandascore.co/images/team/image/1/default.svg",
            "team1_logo_fallback_url": (
                "https://cdn-api.pandascore.co/images/team/image/1/fallback.png"
            ),
        }
    )
    requested = []

    def fake_fetch(url):
        requested.append(url)
        if url.endswith("default.svg"):
            raise media_cards.MediaCardError("unsupported primary logo")
        if url.endswith("fallback.png"):
            return Image.new("RGBA", (20, 20), (255, 0, 0, 255))
        return None

    monkeypatch.setattr(media_cards, "fetch_team_logo", fake_fetch)

    media_cards.render_schedule_card(
        [match],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert requested[:2] == [
        "https://cdn-api.pandascore.co/images/team/image/1/default.svg",
        "https://cdn-api.pandascore.co/images/team/image/1/fallback.png",
    ]


def test_compact_schedule_requests_team_logos_for_every_match(monkeypatch):
    requested = []
    matches = []
    for index in range(4):
        matches.append(
            _upcoming(str(index)).model_copy(
                update={
                    "team1_logo_url": (
                        f"https://cdn.pandascore.co/images/team/image/{index * 2 + 1}/left.png"
                    ),
                    "team2_logo_url": (
                        f"https://cdn.pandascore.co/images/team/image/{index * 2 + 2}/right.png"
                    ),
                }
            )
        )

    def fake_fetch(url):
        requested.append(url)
        return Image.new("RGBA", (20, 20), (255, 255, 255, 255))

    monkeypatch.setattr(media_cards, "fetch_team_logo", fake_fetch)

    media_cards.render_schedule_card(
        matches,
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert len(requested) == 8
    assert all(url.startswith("https://cdn.pandascore.co/images/team/image/") for url in requested)
