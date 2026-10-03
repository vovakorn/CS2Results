"""Regression checks for mobile text bounds and complete paginated data."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

import pytest
from PIL import Image, ImageDraw
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import (UpcomingMatchNormalized, MatchNormalized,
    RadarBracketNode, TournamentPlacement, TournamentRadar, TournamentVRSImpact, MapResult)

NOW = datetime(2026, 10, 1, 9, tzinfo=ZoneInfo("Europe/Moscow"))


def upcoming(i=0, **changes):
    data = dict(match_id=str(i), tournament_name="BLAST Open Porto 2026",
                competition_key="BLAST Open Porto 2026", team1_name="Gaimin Gladiators Academy",
                team2_name="Natus Vincere Junior", scheduled_at=(NOW + timedelta(minutes=i)).isoformat(), best_of=3)
    data.update(changes)
    return UpcomingMatchNormalized(**data)


def result(i=0, **changes):
    data = dict(source="pandascore", match_id=str(i), tournament_name="BLAST Open Porto 2026",
                team1_name="Gaimin Gladiators Academy", team2_name="Natus Vincere Junior",
                score1=2, score2=1, date=(NOW + timedelta(minutes=i)).isoformat())
    data.update(changes)
    return MatchNormalized(**data)


@pytest.mark.parametrize("count", [1, 4, 10, 20])
def test_schedule_pages_retain_every_fixture_and_chronology(count):
    fixtures = [upcoming(i) for i in reversed(range(count))]
    pages = m.paginate_schedule_matches(fixtures)
    assert len(pages) <= 10
    assert all(1 <= len(page) <= 4 for page in pages)
    assert [x.match_id for page in pages for x in page] == [str(i) for i in range(count)]


def test_many_interleaved_tournaments_fit_platform_limit_without_losing_ownership():
    fixtures = [upcoming(i, competition_key=f"Event {i%3}", tournament_name=f"Event {i%3}") for i in range(20)]
    pages = m.paginate_schedule_matches(fixtures)
    assert len(pages) <= 10
    assert [x.match_id for page in pages for x in page] == [str(i) for i in range(20)]
    assert [x.competition_key for page in pages for x in page] == [x.competition_key for x in fixtures]


def test_results_sources_stay_separate_and_single_page_cannot_mix_them():
    matches = [result(0), result(1, source="liquipedia")]
    with pytest.raises(m.MediaCardError, match="mix sources"):
        m.render_results_card(matches, NOW)
    assert len(m.render_results_cards(matches, NOW)) == 2


def test_radar_all_48_slots_and_every_predecessor_survive_pagination():
    nodes = [RadarBracketNode(match_id=f"node-{i}", round_name=f"Round {i//6}",
             previous_match_ids=[] if i == 0 else [f"node-{i-1}"]) for i in range(48)]
    pages, references = m._paginate_radar_bracket_nodes(list(reversed(nodes)))
    assert len(pages) <= 10
    assert [n.match_id for page in pages for n in page] == [n.match_id for n in nodes]
    numbers = {n.match_id: f"{i:02d}" for i, n in enumerate(nodes, 1)}
    for node in nodes[1:]:
        assert "ИЗ " + numbers[node.previous_match_ids[0]] in references[node.match_id]
    assert "С.1" in references["node-6"]


def test_radar_duplicate_ids_or_cycles_are_rejected_instead_of_inventing_routes():
    with pytest.raises(m.MediaCardError, match="duplicate"):
        m._paginate_radar_bracket_nodes([RadarBracketNode(match_id="a"), RadarBracketNode(match_id="a")])
    with pytest.raises(m.MediaCardError, match="cyclic"):
        m._paginate_radar_bracket_nodes([RadarBracketNode(match_id="a", previous_match_ids=["b"]),
                                      RadarBracketNode(match_id="b", previous_match_ids=["a"])])


def test_readable_floor_does_not_allow_overflow():
    draw = ImageDraw.Draw(Image.new("RGB", (1080, 1080)))
    text = "VERY LONG INTERNATIONAL ESPORTS ACADEMY" * 4
    font = m._fit_font(draw, text, 200, 40, 36)
    bounded = m._bounded_text(draw, text, font)
    box = draw.textbbox((0, 0), bounded, font=font)
    assert font.size >= 36 and box[2] - box[0] <= 200 and bounded.endswith("…")


@pytest.mark.parametrize("renderer", ["schedule", "digest", "reel"])
def test_known_long_names_are_complete_and_key_text_stays_inside_image(monkeypatch, renderer):
    calls = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        image = draw._image
        font = kwargs.get("font")
        if image.mode != "L" and image.width == 1080 and font is not None:
            box = draw.textbbox(xy, text, font=font)
            calls.append((text, font.size, box, image.height))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    if renderer == "schedule":
        m.render_schedule_card([upcoming(i) for i in range(4)], NOW, "Europe/Moscow")
    elif renderer == "digest":
        m.render_results_card([result(i) for i in range(4)], NOW)
    else:
        r.render_scene(r.storyboard([upcoming(i) for i in range(4)])[1], NOW, 4)
    names = [x for x in calls if any(word in x[0] for word in ["GAIMIN", "GLADIATORS", "ACADEMY", "NATUS", "JUNIOR"])]
    text = " ".join(x[0] for x in names)
    assert "GAIMIN GLADIATORS ACADEMY" in text and "NATUS VINCERE JUNIOR" in text
    assert all(size >= 36 for _, size, _, _ in names)
    assert all(0 <= box[0] <= box[2] <= 1080 and 0 <= box[1] <= box[3] <= height
               for _, _, box, height in calls)


def test_russian_display_and_latin_data_fonts_cover_used_glyphs():
    for font, text in [(m._font(40, display=True), "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯё№…–—"),
                       (m._font(40), "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$:+-…–—")]:
        missing = font.getmask(chr(0x10ffff))
        for char in text:
            glyph = font.getmask(char)
            assert (glyph.size, bytes(glyph)) != (missing.size, bytes(missing)), char


def test_maximum_prize_amount_is_not_ellipsized(monkeypatch):
    texts = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        texts.append(text)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    m.render_tournament_standings_cards("IEM", [TournamentPlacement(placement=str(i+1),
        team_name="Gaimin Gladiators Academy", prize_usd=10_000_000_000) for i in range(8)])
    assert "$10000000000" in "".join(texts).replace(" ", "")


def test_chronology_uses_instants_when_iso_offsets_differ():
    later = upcoming(0, scheduled_at="2026-10-01T10:00:00Z")
    earlier = upcoming(1, scheduled_at="2026-10-01T12:00:00+03:00")
    assert [x.match_id for page in m.paginate_schedule_matches([later, earlier]) for x in page] == ["1", "0"]
    assert m._fixture_timestamp(earlier.scheduled_at) < m._fixture_timestamp(later.scheduled_at)


@pytest.mark.parametrize("template", ["schedule", "result", "digest", "radar", "standings", "vrs",
                                      "context", "final", "reel_intro", "reel_matches", "reel_outro"])
def test_every_template_has_a_centered_emblem_and_readable_name_below_it(monkeypatch, template):
    texts, logos = [], []
    original_text = ImageDraw.ImageDraw.text
    original_logo = m._draw_channel_logo
    def capture_text(draw, xy, text, *args, **kwargs):
        if draw._image.width == 1080 and draw._image.mode != "L" and kwargs.get("font"):
            texts.append((text, draw.textbbox(xy, text, font=kwargs["font"]), kwargs["font"].size))
        return original_text(draw, xy, text, *args, **kwargs)
    def capture_logo(canvas, draw, center, diameter):
        logos.append((center, diameter))
        return original_logo(canvas, draw, center, diameter)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(m, "_draw_channel_logo", capture_logo)
    if template == "schedule":
        m.render_schedule_card([upcoming(i) for i in range(4)], NOW, "Europe/Moscow")
    elif template == "result":
        m.render_result_card(result())
    elif template == "digest":
        m.render_results_card([result(i) for i in range(4)], NOW)
    elif template == "radar":
        m.render_tournament_radar_cards(TournamentRadar(tournament_id="test", next_matches=[upcoming()]),
                                        "BLAST Open Porto 2026", "Europe/Moscow")
    elif template == "standings":
        m.render_tournament_standings_cards("BLAST Open Porto 2026", [TournamentPlacement(
            placement=str(i+1), team_name="Team Spirit", prize_usd=500000) for i in range(8)])
    elif template == "vrs":
        m.render_tournament_vrs_cards("BLAST Open Porto 2026", [TournamentVRSImpact(
            placement=str(i+1), team_name="Team Spirit", team_id=str(i), before_points=1000,
            after_points=1020, before_rank=i+2, after_rank=i+1, points_delta=20, rank_delta=1,
            source="Valve", before_version="before", after_version="after") for i in range(8)])
    elif template == "context":
        m.render_schedule_context_covers([upcoming()], NOW)
    elif template == "final":
        m.render_final_card(result(source="liquipedia", is_final=True,
            winner_prize_usd=500000, maps=[MapResult(name=n, score1=a, score2=b)
                for n,a,b in [("Mirage",13,9),("Nuke",9,13),("Ancient",13,8)]]))
    else:
        index = {"reel_intro":0, "reel_matches":1, "reel_outro":2}[template]
        r.render_scene(r.storyboard([upcoming(i) for i in range(4)])[index], NOW, 4)
    assert len(logos) == 1
    (center_x, center_y), diameter = logos[0]
    assert center_x == 540
    brands = [(box,size) for text,box,size in texts if text == "CS2 RESULTS"]
    assert len(brands) == 1
    box, size = brands[0]
    assert size >= 38 and (box[0]+box[2])/2 == 540
    assert box[1] > center_y + diameter/2 + 5
    assert all(0 <= b[0] <= b[2] <= 1080 and 0 <= b[1] <= b[3] <= (1920 if template.startswith("reel") else 1080)
               for _, b, _ in texts)


def test_missing_tournament_logo_keeps_tournament_text_centered(monkeypatch):
    labels = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        if text == "BLAST OPEN PORTO 2026":
            labels.append(draw.textbbox(xy, text, font=kwargs["font"]))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    monkeypatch.setattr(m, "_draw_tournament_logo", lambda *args: False)
    m.render_schedule_card([upcoming(tournament_logo_url="https://cdn.pandascore.co/images/league/image/1/x.png")],
                            NOW, "Europe/Moscow")
    assert len(labels) == 1 and (labels[0][0] + labels[0][2])/2 == 540


@pytest.mark.parametrize("template", ["schedule", "result", "digest", "radar"])
def test_single_match_long_names_remain_complete_and_clear_of_logos(monkeypatch, template):
    texts, logos, panels = [], [], []
    original_text = ImageDraw.ImageDraw.text
    original_logo = m._draw_logo
    original_rectangle = ImageDraw.ImageDraw.rounded_rectangle

    def capture_text(draw, xy, text, *args, **kwargs):
        if draw._image.mode != "L" and kwargs.get("font"):
            texts.append((text, draw.textbbox(xy, text, font=kwargs["font"]), kwargs["font"].size))
        return original_text(draw, xy, text, *args, **kwargs)

    def capture_logo(canvas, draw, center, diameter, *args, **kwargs):
        logos.append((center, diameter))
        return original_logo(canvas, draw, center, diameter, *args, **kwargs)

    def capture_rectangle(draw, box, *args, **kwargs):
        if kwargs.get("fill") == (*m.PANEL, 240):
            panels.append(box)
        return original_rectangle(draw, box, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", capture_rectangle)
    monkeypatch.setattr(m, "_draw_logo", capture_logo)
    if template == "schedule":
        m.render_schedule_card([upcoming()], NOW, "Europe/Moscow")
    elif template == "result":
        m.render_result_card(result())
    elif template == "digest":
        m.render_results_card([result()], NOW)
    else:
        m.render_tournament_radar_card(TournamentRadar(tournament_id="t", next_matches=[upcoming()]),
            "BLAST Open Porto 2026", "Europe/Moscow", "next_match")
    assert len(panels) == 1 and len(logos) == 2
    x0, y0, x1, y1 = panels[0]
    assert y1 - y0 <= (465 if template in {"result", "digest"} else 420)
    for name, words, (center, diameter) in [
        ("GAIMIN GLADIATORS ACADEMY", {"GAIMIN", "GLADIATORS", "ACADEMY"}, logos[0]),
        ("NATUS VINCERE JUNIOR", {"NATUS VINCERE", "JUNIOR"}, logos[1]),
    ]:
        lines = [(text, box, size) for text, box, size in texts if text in words]
        assert " ".join(text for text, _, _ in lines) == name
        assert all(size >= 48 for _, _, size in lines)
        assert min(box[1] for _, box, _ in lines) >= center[1] + diameter / 2 + 20
        assert all(x0 <= box[0] < box[2] <= x1 and y0 <= box[1] < box[3] <= y1 - 36
                   for _, box, _ in lines)
