import io

import pytest
from PIL import Image, ImageDraw

from cs2bot import media_cards
from cs2bot.match_sources.models import (
    MapResult,
    MatchNormalized,
    RadarBracketMatch,
    RadarBracketNode,
    RadarStandingTeam,
    TournamentPlacement,
    TournamentRadar,
    UpcomingMatchNormalized,
)


@pytest.fixture(autouse=True)
def clear_logo_memory_cache():
    media_cards._logo_memory_cache.clear()
    yield
    media_cards._logo_memory_cache.clear()



@pytest.fixture
def drawn_text(monkeypatch):
    from PIL import ImageDraw
    calls = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        calls.append((xy, text, kwargs.get("font"), kwargs.get("fill")))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    return calls


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


def test_schedule_context_cover_is_valid_square_png_and_groups_stages():
    group_a = _upcoming("group-a").model_copy(
        update={"tournament_name": "BLAST Open — Porto — Group A", "competition_key": "BLAST Open — Porto"}
    )
    group_b = _upcoming("group-b").model_copy(
        update={
            "tournament_name": "BLAST Open — Porto — Group B",
            "competition_key": "BLAST Open — Porto",
            "scheduled_at": "2026-07-31T17:00:00Z",
        }
    )

    covers = media_cards.render_schedule_context_covers(
        [group_a, group_b],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
    )

    image = Image.open(io.BytesIO(covers[0]))
    assert len(covers) == 1
    assert image.format == "PNG"
    assert image.size == media_cards.SCHEDULE_CONTEXT_COVER_SIZE


def test_schedule_context_cover_keeps_full_tournament_name_when_key_is_only_a_city():
    match = _upcoming().model_copy(
        update={
            "tournament_name": "BLAST Open — Porto — Group B",
            "competition_key": "Porto",
        }
    )

    assert media_cards._schedule_tournament_header([match]) == ("BLAST Open — Porto", None)


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


@pytest.mark.parametrize("map_count", [3, 4, 5])
def test_final_card_adapts_to_three_four_and_five_maps(map_count):
    maps = [
        MapResult(name=name, score1=13, score2=9 + index)
        for index, name in enumerate(("Mirage", "Dust II", "Nuke", "Ancient", "Inferno")[:map_count])
    ]
    match = _result().model_copy(update={
        "is_final": True,
        "winner_prize_usd": 500_000,
        "maps": maps,
    })

    image = Image.open(io.BytesIO(media_cards.render_final_card(match)))

    assert image.format == "PNG"
    assert image.size == media_cards.RESULT_CARD_SIZE



@pytest.mark.parametrize("map_count", [3, 4, 5])
@pytest.mark.parametrize("prize_usd", [0, 500_000, 10_000_000_000])
def test_final_prize_label_and_amount_are_centered_in_their_cells(
    monkeypatch, drawn_text, map_count, prize_usd,
):
    from PIL import ImageDraw

    panels = []
    original_panel = media_cards._draw_final_foil_panel

    def capture_panel(canvas, box, **kwargs):
        panels.append(box)
        return original_panel(canvas, box, **kwargs)

    monkeypatch.setattr(media_cards, "_draw_final_foil_panel", capture_panel)
    match = _result().model_copy(update={
        "source": "liquipedia", "is_final": True,
        "final_identity_confirmed": True, "winner_prize_usd": prize_usd,
        "maps": [MapResult(name=name, score1=13, score2=9)
                 for name in ("Mirage", "Dust II", "Nuke", "Ancient", "Inferno")[:map_count]],
    })
    image = Image.open(io.BytesIO(media_cards.render_final_card(match)))
    measure = ImageDraw.Draw(image)
    label = [measure.textbbox(xy, text, font=font) for xy, text, font, _ in drawn_text
             if text in {"ПРИЗОВЫЕ", "ПОБЕДИТЕЛЯ"}]
    amount = [measure.textbbox(xy, text, font=font) for xy, text, font, _ in drawn_text
              if text == media_cards._format_usd(prize_usd)]
    assert len(label) == 2
    assert len(amount) == 1
    label_box = (min(b[0] for b in label), min(b[1] for b in label),
                 max(b[2] for b in label), max(b[3] for b in label))
    x0, y0, x1, y1 = panels[-1]
    divider = (x0 + x1) / 2
    for box, left, right in [(label_box, x0, divider), (amount[0], divider, x1)]:
        assert (box[0] + box[2]) / 2 == pytest.approx((left + right) / 2, abs=1)
        assert (box[1] + box[3]) / 2 == pytest.approx((y0 + y1) / 2, abs=1)
        assert left + 16 <= box[0] < box[2] <= right - 16
        assert y0 + 12 <= box[1] < box[3] <= y1 - 12

def test_final_card_places_compact_logos_above_team_names(monkeypatch):
    match = _result().model_copy(update={
        "is_final": True,
        "winner_prize_usd": 500_000,
        "maps": [
            MapResult(name="Mirage", score1=13, score2=9),
            MapResult(name="Nuke", score1=13, score2=11),
            MapResult(name="Ancient", score1=13, score2=10),
        ],
        "team1_logo_url": "https://cdn.pandascore.co/images/team/image/1/left.png",
        "team1_logo_fallback_url": "https://cdn.pandascore.co/images/team/image/1/left-fallback.png",
        "team2_logo_url": "https://cdn.pandascore.co/images/team/image/2/right.png",
        "team2_logo_fallback_url": "https://cdn.pandascore.co/images/team/image/2/right-fallback.png",
    })
    logos = []

    def capture_logo(
        canvas,
        draw,
        center,
        diameter,
        team_name,
        logo_url,
        accent,
        fallback_logo_url=None,
        *,
        content_scale=0.64,
    ):
        logos.append((center, diameter, team_name, logo_url, fallback_logo_url, content_scale))

    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    media_cards.render_final_card(match)

    assert logos == [
        ((220, 362), 72, "3DMAX", match.team1_logo_url, match.team1_logo_fallback_url, 0.76),
        ((860, 362), 72, "MOUZ", match.team2_logo_url, match.team2_logo_fallback_url, 0.76),
    ]


def test_final_card_renders_when_team_logos_are_unavailable(monkeypatch):
    match = _result().model_copy(update={
        "is_final": True,
        "winner_prize_usd": 500_000,
        "maps": [
            MapResult(name="Mirage", score1=13, score2=9),
            MapResult(name="Nuke", score1=13, score2=11),
            MapResult(name="Ancient", score1=13, score2=10),
        ],
        "team1_logo_url": "https://cdn.pandascore.co/images/team/image/1/left.png",
        "team2_logo_url": "https://cdn.pandascore.co/images/team/image/2/right.png",
    })
    monkeypatch.setattr(media_cards, "fetch_team_logo", lambda url: None)

    image = Image.open(io.BytesIO(media_cards.render_final_card(match)))

    assert image.size == media_cards.RESULT_CARD_SIZE


def test_final_card_uses_one_gold_foil_header_accent(monkeypatch):
    match = _result().model_copy(update={
        "is_final": True,
        "winner_prize_usd": 500_000,
        "maps": [
            MapResult(name="Mirage", score1=13, score2=9),
            MapResult(name="Nuke", score1=13, score2=11),
            MapResult(name="Ancient", score1=13, score2=10),
        ],
    })
    original = media_cards._background
    captured = {}

    def capture_background(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(media_cards, "_background", capture_background)
    media_cards.render_final_card(match)

    assert captured["header_accent_colors"] == (media_cards.FINAL_GOLD, media_cards.FINAL_GOLD)
    assert captured["header_foil"] is True


def test_tournament_standings_cards_render_all_placements_in_an_album():
    placements = [
        TournamentPlacement(
            placement=str(index + 1),
            team_name=f"Team {index + 1}",
            prize_usd=(12 - index) * 25_000,
        )
        for index in range(12)
    ]

    cards = media_cards.render_tournament_standings_cards("IEM Cologne 2026", placements)

    assert len(cards) == 2
    assert all(Image.open(io.BytesIO(card)).size == media_cards.RESULT_CARD_SIZE for card in cards)
    assert media_cards.can_render_tournament_standings(placements)


def test_tournament_standings_require_payout_for_every_team():
    placements = [
        TournamentPlacement(placement="1", team_name="NAVI", prize_usd=500_000),
        TournamentPlacement(placement="2", team_name="FaZe", prize_usd=None),
    ]

    assert not media_cards.can_render_tournament_standings(placements)
    with pytest.raises(media_cards.MediaCardError, match="complete placements"):
        media_cards.render_tournament_standings_cards("IEM Cologne 2026", placements)


def test_tournament_standings_reject_odd_team_count():
    placements = [
        TournamentPlacement(placement=str(index + 1), team_name=f"Team {index + 1}", prize_usd=10_000)
        for index in range(5)
    ]

    assert not media_cards.can_render_tournament_standings(placements)
    with pytest.raises(media_cards.MediaCardError, match="complete placements"):
        media_cards.render_tournament_standings_cards("BLAST Open Porto", placements)


def test_tournament_standings_use_bronze_only_for_third_or_shared_third_fourth():
    assert media_cards._standings_row_color("1") == media_cards.STANDINGS_GOLD
    assert media_cards._standings_row_color("2") == media_cards.STANDINGS_SILVER
    assert media_cards._standings_row_color("3") == media_cards.STANDINGS_BRONZE
    assert media_cards._standings_row_color("4") == media_cards.WHITE
    assert media_cards._standings_row_color("3–4") == media_cards.STANDINGS_BRONZE
    assert media_cards._standings_row_color("3-4") == media_cards.STANDINGS_BRONZE


@pytest.mark.parametrize("placement,has_medal", [("3", True), ("4", False),
                                                ("3–4", True), ("3-4", True)])
def test_standings_medal_respects_separate_and_shared_places(placement, has_medal):
    from PIL import ImageDraw
    canvas = Image.new("RGBA", (300, 100), (0, 0, 0, 255))
    media_cards._draw_standings_medal(canvas, ImageDraw.Draw(canvas), 146, 0,
                                     placement, row_height=68)
    assert (canvas.getpixel((146, 13)) != (0, 0, 0, 255)) == has_medal


def test_tournament_standings_draws_metallic_podium_medals_and_a_distinct_header():
    placements = [
        TournamentPlacement(placement="1", team_name="Spirit", prize_usd=150_000),
        TournamentPlacement(placement="2", team_name="MOUZ", prize_usd=60_000),
        TournamentPlacement(placement="3–4", team_name="Vitality", prize_usd=40_000),
        TournamentPlacement(placement="3–4", team_name="Falcons", prize_usd=40_000),
    ]

    placements.extend(TournamentPlacement(placement=str(i), team_name=f"Team {i}", prize_usd=1000)
                      for i in range(5, 9))
    image = Image.open(
        io.BytesIO(media_cards.render_tournament_standings_cards("BLAST Open Porto", placements)[0])
    ).convert("RGB")
    table_top = 342 + (media_cards.TOURNAMENT_STANDINGS_PER_CARD - len(placements)) * 26
    first_row_top = table_top + 72

    for index in range(4):
        row_top = first_row_top + index * 68
        shadow = image.getpixel((146, row_top + 13))
        sheen = image.getpixel((146, row_top + 22))
        assert sum(sheen) > sum(shadow)
    assert image.getpixel((146, first_row_top + 22)) != image.getpixel((146, first_row_top + 68 + 22))
    assert image.getpixel((146, first_row_top + 68 + 22)) != image.getpixel((146, first_row_top + 2 * 68 + 22))
    assert image.getpixel((136, first_row_top + 17)) != image.getpixel((156, first_row_top + 17))
    bronze_row_top = first_row_top + 2 * 68
    assert image.getpixel((120, bronze_row_top + 23)) != image.getpixel((172, bronze_row_top + 23))
    assert image.getpixel((540, table_top + 30)) == media_cards.STANDINGS_HEADER
    assert image.getpixel((540, table_top)) == media_cards.STANDINGS_HEADER_LINE
    assert image.getpixel((540, table_top + 72)) == media_cards.STANDINGS_HEADER_LINE


def test_tournament_standings_use_the_source_label_on_every_page(drawn_text):
    placements = [TournamentPlacement(placement=str(i + 1), team_name=f"Team {i}", prize_usd=100) for i in range(10)]
    media_cards.render_tournament_standings_cards("IEM", placements, source_label="BLAST.tv")
    assert [v for _, v, _, _ in drawn_text].count("ИСТОЧНИК: BLAST.TV") == 2


def test_result_card_channel_logo_is_inside_header(monkeypatch):
    drawn = []
    monkeypatch.setattr(media_cards, "_draw_channel_logo", lambda c, d, center, diameter: drawn.append((center, diameter)))
    media_cards.render_result_card(_result())
    assert len(drawn) == 1
    (x, y), diameter = drawn[0]
    assert x == 540 and diameter >= 80 and 0 <= x - diameter/2 < x + diameter/2 <= 1080 and y + diameter/2 < 144


def test_result_card_omits_winner_footer(monkeypatch):
    texts = []
    original = media_cards._centered_text

    def capture_text(draw, center_x, y, text, font, fill):
        texts.append(text)
        return original(draw, center_x, y, text, font, fill)

    monkeypatch.setattr(media_cards, "_centered_text", capture_text)

    media_cards.render_result_card(_result())

    assert not any("ПОБЕДИТЕЛЬ" in text for text in texts)
    assert not any("РЕЗУЛЬТАТ ЗАВЕРШЁН" in text for text in texts)


def test_result_card_preserves_long_team_names_under_logos(drawn_text):
    match = _result().model_copy(update={"team1_name":"Natus Vincere Junior", "team2_name":"Gaimin Gladiators Academy"})
    media_cards.render_result_card(match)
    values = " ".join(v for _, v, _, _ in drawn_text)
    assert "NATUS VINCERE JUNIOR" in values
    assert "GAIMIN GLADIATORS ACADEMY" in values


@pytest.mark.parametrize("match_count", [1, 2, 4, 6, 8, 10])
def test_daily_results_album_supports_requested_match_counts(match_count):
    matches = [_result().model_copy(update={"match_id": str(i)}) for i in range(match_count)]
    cards = media_cards.render_results_cards(matches, media_cards.datetime.fromisoformat("2026-08-01T23:00:00+03:00"))
    assert len(cards) == (match_count + 3) // 4
    assert all(Image.open(io.BytesIO(card)).size == (1080, 1080) for card in cards)


def test_daily_results_album_rejects_more_than_ten_matches():
    with pytest.raises(media_cards.MediaCardError):
        media_cards.render_results_cards([_result() for i in range(11)], media_cards.datetime.now())


def test_four_match_daily_results_use_full_width_readable_rows(monkeypatch):
    boxes = []
    original = media_cards._draw_fixture_row
    def capture(canvas, draw, match, box, **kwargs):
        boxes.append(box)
        return original(canvas, draw, match, box, **kwargs)
    monkeypatch.setattr(media_cards, "_draw_fixture_row", capture)
    media_cards.render_results_card([_result() for i in range(4)], media_cards.datetime.now())
    assert len(boxes) == 4
    assert all(x1 - x0 >= 900 and y1 - y0 >= 145 for x0, y0, x1, y1 in boxes)


def test_schedule_card_is_valid_square_png():
    data = media_cards.render_schedule_card(
        [_upcoming()],
        media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"),
        "Europe/Moscow",
    )

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size == media_cards.SCHEDULE_CARD_SIZE


def test_schedule_card_identifies_format_and_channel(drawn_text):
    media_cards.render_schedule_card([_upcoming()], media_cards.datetime.now(), "Europe/Moscow")
    values = [v for _, v, _, _ in drawn_text]
    assert "МАТЧИ CS2 СЕГОДНЯ" in values and "@CS2_RESULTS" in values


@pytest.mark.parametrize("variant", ["bracket", "next_match"])
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


def test_tournament_radar_bracket_is_paginated_into_square_pngs():
    matches = [
        RadarBracketMatch(
            match_id=f"match-{index}",
            round_name="Opening round",
            team1_name=f"Team {index * 2 + 1}",
            team2_name=f"Team {index * 2 + 2}",
        )
        for index in range(9)
    ]
    radar = TournamentRadar(tournament_id="3", bracket_matches=matches, bracket_match_count=9)

    cards = media_cards.render_tournament_radar_cards(
        radar, "IEM Cologne 2026", "Europe/Moscow", "bracket"
    )

    assert len(cards) == 2
    assert all(Image.open(io.BytesIO(card)).size == media_cards.SCHEDULE_CARD_SIZE for card in cards)


def test_tournament_radar_single_pair_uses_large_match_composition(monkeypatch):
    match = RadarBracketMatch(match_id="group-1", round_name="Group stage", team1_name="NAVI", team2_name="FaZe")
    radar = TournamentRadar(tournament_id="3", bracket_matches=[match], bracket_match_count=1)
    logo_sizes = []
    original = media_cards._draw_logo

    def capture_logo(canvas, draw, center, diameter, *args, **kwargs):
        logo_sizes.append(diameter)
        return original(canvas, draw, center, diameter, *args, **kwargs)

    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    data = media_cards.render_tournament_radar_card(radar, "IEM Cologne 2026", "Europe/Moscow", "bracket")

    assert len(logo_sizes) == 2 and min(logo_sizes) >= 140
    assert media_cards._radar_bracket_content_label([match]) == "ПОДТВЕРЖДЁННАЯ ПАРА"
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)


def test_tournament_radar_four_pairs_get_large_non_overlapping_cards(monkeypatch):
    matches = [
        RadarBracketMatch(
            match_id=f"group-{index}",
            round_name="Group stage",
            team1_name=f"Natus Vincere International Squad {index}",
            team2_name=f"Team Counterstrike Academy {index}",
        )
        for index in range(4)
    ]
    boxes = []
    original = media_cards._draw_radar_bracket_match

    def capture_match(canvas, draw, match, box, **kwargs):
        boxes.append(box)
        return original(canvas, draw, match, box, **kwargs)

    monkeypatch.setattr(media_cards, "_draw_radar_bracket_match", capture_match)
    media_cards.render_tournament_radar_cards(
        TournamentRadar(tournament_id="3", bracket_matches=matches, bracket_match_count=4),
        "IEM Cologne 2026",
        "Europe/Moscow",
        "bracket",
    )

    assert len(boxes) == 4
    assert min(y1 - y0 for _, y0, _, y1 in boxes) >= 230
    assert all(326 <= y0 < y1 <= 964 for _, y0, _, y1 in boxes)
    for index, left in enumerate(boxes):
        for right in boxes[index + 1:]:
            assert left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1]


def test_tournament_radar_dense_structure_keeps_local_links_on_readable_pages():
    opening = [RadarBracketMatch(match_id=f"opening-{i}", team1_name=f"Team Alpha {i}",
                                team2_name=f"Team Beta {i}") for i in range(8)]
    next_round = [RadarBracketNode(match_id=f"quarter-{i}",
                  previous_match_ids=[f"opening-{2*i}", f"opening-{2*i+1}"]) for i in range(4)]
    structure = [*opening, *next_round]
    pages, references = media_cards._paginate_radar_bracket_nodes(structure)
    cards = media_cards.render_tournament_radar_cards(
        TournamentRadar(tournament_id="3", bracket_matches=opening, bracket_structure=structure),
        "IEM Cologne 2026", "Europe/Moscow", "bracket")
    assert len(pages) == len(cards) == 2
    assert all(len(page) <= 6 for page in pages)
    assert [node.match_id for page in pages for node in page] == [node.match_id for node in structure]
    for i, node in enumerate(next_round):
        assert f"{2*i+1:02d}" in references[node.match_id]
        assert f"{2*i+2:02d}" in references[node.match_id]
    assert "С.1" in references["quarter-0"]
    assert all(Image.open(io.BytesIO(card)).size == (1080, 1080) for card in cards)


def test_tournament_radar_large_structures_stay_within_telegram_album_limit(monkeypatch):
    flat = [RadarBracketMatch(match_id=str(i), team1_name=f"Team {i}", team2_name=f"Opponent {i}")
            for i in range(48)]
    chain = [RadarBracketNode(match_id=str(i), previous_match_ids=[str(i-1)] if i else []) for i in range(48)]
    for nodes in (flat, chain):
        pages, _ = media_cards._paginate_radar_bracket_nodes(nodes)
        assert len(pages) == 8
        assert sorted(node.match_id for page in pages for node in page) == sorted(node.match_id for node in nodes)
    boxes = []
    original = media_cards._draw_radar_bracket_match
    def capture_match(canvas, draw, match, box, **kwargs):
        boxes.append(box)
        return original(canvas, draw, match, box, **kwargs)
    monkeypatch.setattr(media_cards, "_draw_radar_bracket_match", capture_match)
    cards = media_cards.render_tournament_radar_cards(
        TournamentRadar(tournament_id="3", bracket_structure=chain, bracket_match_count=48),
        "IEM Cologne 2026", "Europe/Moscow", "bracket")
    assert len(cards) == 8 and len(boxes) == 48
    assert all(x1-x0 >= 450 and y1-y0 >= 200 for x0,y0,x1,y1 in boxes)


@pytest.mark.parametrize("as_album", [False, True])
@pytest.mark.parametrize("has_future_slot", [False, True])
def test_tournament_radar_labels_tournament_content_without_assuming_playoffs(monkeypatch, as_album, has_future_slot):
    opening = RadarBracketMatch(
        match_id="group-1",
        round_name="Group stage",
        team1_name="NAVI",
        team2_name="FaZe",
    )
    second = RadarBracketMatch(
        match_id="group-2",
        round_name="Group stage",
        team1_name="Spirit",
        team2_name="Vitality",
    )
    radar = TournamentRadar(tournament_id="3", bracket_matches=[opening, second], bracket_match_count=2)
    if has_future_slot:
        radar.bracket_structure = [
            opening,
            second,
            RadarBracketNode(match_id="group-final", round_name="Group final", previous_match_ids=["group-1", "group-2"]),
        ]

    drawn_text = []
    original_text = ImageDraw.ImageDraw.text
    def capture_text(draw, xy, text, *args, **kwargs):
        if draw._image.width == 1080:
            drawn_text.append(text)
        return original_text(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    render = media_cards.render_tournament_radar_cards if as_album else media_cards.render_tournament_radar_card
    render(radar, "IEM Cologne 2026 — Group stage", "Europe/Moscow", "bracket")

    expected = "СЕТКА ТУРНИРА" if has_future_slot else "ПОДТВЕРЖДЁННЫЕ ПАРЫ"
    assert sum(text.startswith(expected) for text in drawn_text) == 1
    expected_count = "2 МАТЧА В СЕТКЕ" if has_future_slot else "2 МАТЧА"
    assert any(text.endswith(expected_count) for text in drawn_text)
    assert not any("ПЛЕЙ-ОФФ" in text for text in drawn_text)


def test_tournament_radar_bracket_uses_links_and_team_logos(monkeypatch):
    matches = [
        RadarBracketMatch(
            match_id="opening-1",
            round_name="Opening round",
            team1_name="NAVI",
            team2_name="FaZe",
            team1_logo_url="https://cdn.pandascore.co/images/team/image/10/navi.png",
            team2_logo_url="https://cdn.pandascore.co/images/team/image/20/faze.png",
        ),
        RadarBracketMatch(
            match_id="opening-2",
            round_name="Opening round",
            team1_name="Spirit",
            team2_name="Vitality",
            team1_logo_url="https://cdn.pandascore.co/images/team/image/30/spirit.png",
            team2_logo_url="https://cdn.pandascore.co/images/team/image/40/vitality.png",
        ),
        RadarBracketMatch(
            match_id="final",
            round_name="Upper final",
            team1_name="NAVI",
            team2_name="Spirit",
            previous_match_ids=["opening-1", "opening-2"],
        ),
    ]
    calls = []

    def capture_logo(canvas, draw, center, diameter, team_name, logo_url, *args):
        calls.append((team_name, logo_url))

    monkeypatch.setattr(media_cards, "_draw_logo", capture_logo)
    columns, positions = media_cards._radar_bracket_columns(matches)
    data = media_cards.render_tournament_radar_card(
        TournamentRadar(tournament_id="3", bracket_matches=matches),
        "FISSURE PLAYGROUND — SEASON 3 2026 — GROUP A",
        "Europe/Moscow",
        "bracket",
    )

    assert [[match.match_id for match in column] for column in columns] == [
        ["opening-1", "opening-2"], ["final"]
    ]
    assert positions["opening-1"] < positions["final"]
    assert ("NAVI", "https://cdn.pandascore.co/images/team/image/10/navi.png") in calls
    assert ("Vitality", "https://cdn.pandascore.co/images/team/image/40/vitality.png") in calls
    assert Image.open(io.BytesIO(data)).size == media_cards.SCHEDULE_CARD_SIZE


def test_tournament_radar_shows_future_slots_as_tbd_without_inventing_teams(monkeypatch, drawn_text):
    opening = RadarBracketMatch(
        match_id="opening",
        round_name="Opening round",
        team1_name="NAVI",
        team2_name="FaZe",
        team1_logo_url="https://cdn.pandascore.co/images/team/image/10/navi.png",
        team2_logo_url="https://cdn.pandascore.co/images/team/image/20/faze.png",
    )
    future = RadarBracketNode(
        match_id="upper-final",
        round_name="Upper final",
        previous_match_ids=["opening"],
    )
    rendered_names = []
    original = media_cards._aligned_text

    def capture_text(draw, edge_x, y, text, font, fill, alignment):
        rendered_names.append(text)
        return original(draw, edge_x, y, text, font, fill, alignment)

    monkeypatch.setattr(media_cards, "_aligned_text", capture_text)
    data = media_cards.render_tournament_radar_card(
        TournamentRadar(
            tournament_id="3",
            bracket_matches=[opening],
            bracket_structure=[opening, future],
        ),
        "FISSURE PLAYGROUND — SEASON 3 2026 — GROUP A",
        "Europe/Moscow",
        "bracket",
    )

    assert [v for _, v, _, _ in drawn_text].count("TBD") == 2
    assert "NAVI" in [v for _, v, _, _ in drawn_text]
    assert Image.open(io.BytesIO(data)).size == media_cards.SCHEDULE_CARD_SIZE


def test_schedule_album_supports_ten_matches():
    cards = media_cards.render_schedule_cards([_upcoming(str(i)) for i in range(10)], media_cards.datetime.now(), "Europe/Moscow")
    assert len(cards) == 3
    assert all(Image.open(io.BytesIO(card)).size == (1080, 1080) for card in cards)


def test_single_schedule_page_rejects_more_than_four_matches():
    with pytest.raises(media_cards.MediaCardError, match="four"):
        media_cards.render_schedule_card([_upcoming(str(i)) for i in range(5)], media_cards.datetime.now(), "Europe/Moscow")


def test_schedule_album_preserves_all_sixteen_matches_in_order():
    matches = [_upcoming(str(i)).model_copy(update={"scheduled_at": f"2026-07-31T{i:02d}:00:00Z"}) for i in range(16)]
    pages = media_cards.paginate_schedule_matches(list(reversed(matches)))
    assert [len(page) for page in pages] == [4, 4, 4, 4]
    assert [match.match_id for page in pages for match in page] == [str(i) for i in range(16)]


def test_schedule_album_balances_odd_match_count():
    pages = media_cards.paginate_schedule_matches(
        [_upcoming(str(index)) for index in range(15)]
    )

    assert [len(page) for page in pages] == [4, 4, 4, 3]


def test_schedule_album_renders_four_square_pngs_for_sixteen_matches():
    cards = media_cards.render_schedule_cards(
        [_upcoming(str(index)) for index in range(16)],
        media_cards.datetime.fromisoformat("2026-08-12T10:00:00+03:00"),
        "Europe/Moscow",
    )

    assert len(cards) == 4
    for data in cards:
        image = Image.open(io.BytesIO(data))
        assert image.format == "PNG"
        assert image.size == media_cards.SCHEDULE_CARD_SIZE


def test_schedule_album_marks_page_number(drawn_text):
    media_cards.render_schedule_card([_upcoming(str(i)) for i in range(4)],
        media_cards.datetime.fromisoformat("2026-08-12T10:00:00+03:00"), "Europe/Moscow", page_number=2, page_count=2)
    values = [v for _, v, _, _ in drawn_text]
    assert "12 АВГУСТА · BO3" in values
    assert "2/2" in values


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


def test_schedule_uses_competition_name_for_tournament_header(drawn_text):
    media_cards.render_schedule_card([_upcoming()], media_cards.datetime.now(), "Europe/Moscow")
    values = [v for _, v, _, _ in drawn_text]
    assert "BLAST BOUNTY 2026" in values
    assert "BLAST BOUNTY — 2026 SEASON 2 FINALS" not in values


def test_schedule_match_event_label_falls_back_to_tournament_name():
    match = _upcoming().model_copy(update={"competition_key": None})

    assert media_cards._schedule_match_event_label(match) == match.tournament_name


def test_schedule_header_has_clear_type_event_date_hierarchy(drawn_text):
    media_cards.render_schedule_card([_upcoming()], media_cards.datetime.fromisoformat("2026-07-31T10:00:00+03:00"), "Europe/Moscow")
    values = {v: (xy, font) for xy, v, font, _ in drawn_text}
    assert all(v in values for v in ["МАТЧИ CS2 СЕГОДНЯ", "BLAST BOUNTY 2026", "31 ИЮЛЯ · BO3"])
    assert values["МАТЧИ CS2 СЕГОДНЯ"][0][1] < values["BLAST BOUNTY 2026"][0][1] < values["31 ИЮЛЯ · BO3"][0][1]


@pytest.mark.parametrize("match_count", [1, 4, 10])
def test_schedule_shows_shared_tournament_header_for_every_layout(drawn_text, match_count):
    cards = media_cards.render_schedule_cards([_upcoming(str(i)) for i in range(match_count)], media_cards.datetime.now(), "Europe/Moscow")
    assert [v for _, v, _, _ in drawn_text].count("BLAST BOUNTY 2026") == len(cards)


def test_schedule_album_groups_stages_under_one_tournament_card():
    group_a = _upcoming("group-a").model_copy(
        update={
            "competition_key": "BLAST Open — Porto",
            "tournament_name": "BLAST Open — Porto — Group A",
            "scheduled_at": "2026-07-31T12:00:00Z",
        }
    )
    group_b = _upcoming("group-b").model_copy(
        update={
            "competition_key": "BLAST Open — Porto",
            "tournament_name": "BLAST Open — Porto — Group B",
            "scheduled_at": "2026-07-31T14:00:00Z",
        }
    )
    other = _upcoming("other").model_copy(
        update={
            "competition_key": "IEM Cologne",
            "tournament_name": "IEM Cologne — Playoffs",
            "scheduled_at": "2026-07-31T16:00:00Z",
        }
    )

    pages = media_cards.paginate_schedule_matches([other, group_b, group_a])

    assert [[match.match_id for match in page] for page in pages] == [
        ["group-a", "group-b"],
        ["other"],
    ]


def test_schedule_uses_mixed_tournament_header_without_a_logo(monkeypatch, drawn_text):
    logos = []
    monkeypatch.setattr(media_cards, "_draw_tournament_logo", lambda *a: logos.append(a))
    matches = [_upcoming("one"), _upcoming("two").model_copy(update={"competition_key":"IEM Cologne 2026", "tournament_name":"IEM Cologne 2026"})]
    media_cards.render_schedule_card(matches, media_cards.datetime.now(), "Europe/Moscow")
    assert "ТУРНИРЫ ДНЯ" in [v for _, v, _, _ in drawn_text]
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


def test_schedule_channel_logo_is_inside_header(monkeypatch):
    drawn = []
    monkeypatch.setattr(media_cards, "_draw_channel_logo", lambda c, d, center, diameter: drawn.append((center, diameter)))
    media_cards.render_schedule_card([_upcoming()], media_cards.datetime.now(), "Europe/Moscow")
    assert len(drawn) == 1
    (x, y), diameter = drawn[0]
    assert x == 540 and diameter >= 80 and 0 <= x - diameter/2 < x + diameter/2 <= 1080 and y + diameter/2 < 144


def test_schedule_preserves_complete_long_team_names(drawn_text):
    match = _upcoming().model_copy(update={"team1_name":"Natus Vincere Junior", "team2_name":"Gaimin Gladiators Academy"})
    media_cards.render_schedule_card([match], media_cards.datetime.now(), "Europe/Moscow")
    values = " ".join(v for _, v, _, _ in drawn_text)
    assert "NATUS VINCERE JUNIOR" in values and "GAIMIN GLADIATORS ACADEMY" in values


def test_ten_match_schedule_uses_readable_names_on_every_page(drawn_text):
    matches = [_upcoming(str(i)).model_copy(update={"team1_name":f"Long Left Team {i}","team2_name":f"Long Right Team {i}"}) for i in range(10)]
    media_cards.render_schedule_cards(matches, media_cards.datetime.now(), "Europe/Moscow")
    names = [(v, font) for _, v, font, _ in drawn_text if v.startswith("LONG ")]
    assert len(names) == 20
    assert all(font.size >= 36 for _, font in names)


@pytest.mark.parametrize("match_count", [4, 8, 10])
def test_compact_schedule_rows_remain_readable_across_page_sizes(monkeypatch, match_count):
    boxes = []
    original = media_cards._draw_compact_schedule_match
    def capture(canvas, draw, match, box, tz, **kwargs):
        boxes.append(box)
        return original(canvas, draw, match, box, tz, **kwargs)
    monkeypatch.setattr(media_cards, "_draw_compact_schedule_match", capture)
    media_cards.render_schedule_cards([_upcoming(str(i)) for i in range(match_count)], media_cards.datetime.now(), "Europe/Moscow")
    assert len(boxes) == match_count
    assert all(x1-x0 >= 900 and y1-y0 >= 145 for x0,y0,x1,y1 in boxes)


def test_ten_match_schedule_keeps_logos_inside_rows(monkeypatch):
    boxes, logos = [], []
    original = media_cards._draw_compact_schedule_match
    def capture(c, d, match, box, tz, **kwargs):
        boxes.append(box)
        return original(c, d, match, box, tz, **kwargs)
    monkeypatch.setattr(media_cards, "_draw_compact_schedule_match", capture)
    monkeypatch.setattr(media_cards, "_draw_logo", lambda c,d,center,diameter,*args,**kwargs: logos.append((center,diameter)))
    media_cards.render_schedule_cards([_upcoming(str(i)) for i in range(10)], media_cards.datetime.now(), "Europe/Moscow")
    for i, (x0,y0,x1,y1) in enumerate(boxes):
        for (x,y),diameter in logos[i*2:i*2+2]:
            assert x0 <= x-diameter/2 and x+diameter/2 <= x1
            assert y0 <= y-diameter/2 and y+diameter/2 <= y1


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


@pytest.mark.parametrize("count", [2, 4, 6, 8])
def test_tournament_standings_table_uses_available_height(count, monkeypatch):
    panels = []
    original = media_cards._chamfered_panel

    def capture_panel(draw, box, **kwargs):
        panels.append(box)
        return original(draw, box, **kwargs)

    monkeypatch.setattr(media_cards, "_chamfered_panel", capture_panel)
    placements = [
        TournamentPlacement(placement=str(index + 1), team_name=f"Team {index + 1}", prize_usd=10_000)
        for index in range(count)
    ]
    media_cards.render_tournament_standings_cards("BLAST Open Porto", placements)
    _, top, _, bottom = panels[0]
    assert top >= 340
    assert bottom <= 960
    assert bottom - top >= (340 if count == 2 else 600)

@pytest.mark.parametrize("count", [2, 4, 6])
def test_sparse_standings_keep_long_names_legible_and_champion_visible(count, drawn_text):
    names = ["Gaimin Gladiators Academy", "Natus Vincere Junior"]
    placements = [TournamentPlacement(placement=str(i+1), team_name=names[i%2], prize_usd=500000)
                  for i in range(count)]
    media_cards.render_tournament_standings_cards("BLAST Open Porto", placements)
    texts = " ".join(text for _,text,_,_ in drawn_text)
    assert all(name.upper() in texts for name in names)
    assert "ПОБЕДИТЕЛЬ" in texts if count <= 4 else "ПОБЕДИТЕЛЬ" not in texts
    assert all(font.size >= 36 for _,text,font,_ in drawn_text
               if any(word in text for word in ["GAIMIN", "GLADIATORS", "ACADEMY", "NATUS", "JUNIOR"]))
