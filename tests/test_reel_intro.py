"""Team-day opener: complete input, bounded grid, safe logo paths and no hero."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
import pytest

from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized

NOW = datetime(2026, 10, 4, 9, tzinfo=ZoneInfo("Europe/Moscow"))


def fixtures(count):
    return [UpcomingMatchNormalized(match_id=f"{index:02}", tournament_name="Demo event",
        team1_name=f"Team {2 * index + 1}", team2_name=f"Team {2 * index + 2}",
        scheduled_at=(NOW + timedelta(hours=1, minutes=index * 30)).isoformat())
        for index in range(count)]


def capture_intro(monkeypatch, matches, **kwargs):
    texts, logos = [], []
    original_text, original_logo = r._draw_text_block, r._draw_logo
    def text(*args, **kw):
        texts.append(args[3])
        return original_text(*args, **kw)
    def logo(*args, **kw):
        logos.append((args, kw))
        return original_logo(*args, **kw)
    monkeypatch.setattr(r, "_draw_text_block", text)
    monkeypatch.setattr(r, "_draw_logo", logo)
    scene = r.storyboard(matches)[0]
    image = r.render_scene(scene, NOW, len(matches), **kwargs)
    return scene, image, texts, logos


@pytest.mark.parametrize("count", [1, 4, 10, 20])
def test_intro_shows_all_unique_teams_across_pages_without_changing_the_day(monkeypatch, count):
    matches = list(reversed(fixtures(count)))
    before = [match.model_dump() for match in matches]
    scene, image, texts, logos = capture_intro(monkeypatch, matches)
    assert image.size == (1080, 1920)
    assert scene.matches == () and len(scene.context_matches) == count
    assert len(logos) == min(count * 2, 6)
    assert "ТВОЯ КОМАНДА" in texts and "ИГРАЕТ СЕГОДНЯ?" in texts
    assert f"4 ОКТЯБРЯ · {count} {r._match_noun(count).upper()}" in texts
    assert "СТАРТ В 10:00 МСК" in texts
    assert not any("БЛИЖАЙШИЙ" in text for text in texts)
    intro = [part for part in r.storyboard(matches) if part.kind == "intro"]
    assert len(intro) == (count * 2 + 5) // 6
    assert [team.name for part in intro for team in part.intro_teams] == [team.name for team in r._intro_teams(matches)]
    assert all(1 <= len(part.intro_teams) <= 6 for part in intro)
    assert [part.page for part in intro] == list(range(1, len(intro) + 1))
    if len(intro) > 1:
        assert f"КОМАНДЫ 1 / {len(intro)}" in texts
        for part in intro[1:]:
            capture = []
            original = r._draw_logo
            with monkeypatch.context() as scoped:
                scoped.setattr(r, "_draw_logo", lambda *args, **kwargs:
                    capture.append(args[4]) or original(*args, **kwargs))
                r.render_scene(part, NOW, count)
            assert capture == [team.name for team in part.intro_teams]
    assert [match.model_dump() for match in matches] == before
    assert len([match for part in r.storyboard(matches) for match in part.matches]) == count


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6])
def test_tile_grids_are_centered_and_clear_of_heading_footer_and_each_other(count):
    boxes = r._intro_team_boxes(count)
    assert len(boxes) == count
    for i, (left, top, right, bottom) in enumerate(boxes):
        assert 80 <= left < right <= 920
        assert 800 <= top < bottom <= 1436
        for other in boxes[i + 1:]:
            assert right <= other[0] or other[2] <= left or bottom <= other[1] or other[3] <= top
    for top in {box[1] for box in boxes}:
        row = [box for box in boxes if box[1] == top]
        assert row[0][0] + row[-1][2] == 1000


def test_duplicate_names_merge_available_logos_without_merging_academy_teams():
    first, second = fixtures(2)
    first = first.model_copy(update={"team1_name": "Team Spirit", "team2_name": "NAVI"})
    second = second.model_copy(update={"team1_name": "team   spirit", "team2_name": "Team Spirit Academy",
        "team1_logo_url": "preview://primary", "team1_logo_fallback_url": "preview://fallback"})
    teams = r._intro_teams([second, first])
    assert [team.name for team in teams] == ["Team Spirit", "NAVI", "Team Spirit Academy"]
    assert teams[0].logo_url == "preview://primary" and teams[0].fallback_url == "preview://fallback"


def test_parallel_starts_keep_all_teams_and_a_stable_non_editorial_order(monkeypatch):
    matches = [match.model_copy(update={"scheduled_at": "2026-10-04T10:00:00+03:00"}) for match in fixtures(4)]
    assert r._intro_teams(matches) == r._intro_teams(list(reversed(matches)))
    _, _, texts, logos = capture_intro(monkeypatch, list(reversed(matches)))
    assert "КОМАНДЫ 1 / 2" in texts
    assert [call[0][4] for call in logos] == [f"Team {index}" for index in range(1, 7)]


@pytest.mark.parametrize("count,duration", [(1, 8), (4, 9.5), (10, 20.5), (20, 38)])
def test_intro_pagination_updates_duration_for_distinct_teams(count, duration):
    scenes = r.storyboard(fixtures(count))
    assert sum(scene.duration for scene in scenes) == duration
    assert scenes[0].kind == "intro" and scenes[-1].kind == "outro"


def test_start_uses_real_earliest_instant_in_display_timezone(monkeypatch):
    early, later = fixtures(2)
    early = early.model_copy(update={"scheduled_at": "2026-10-04T06:00:00Z"})
    later = later.model_copy(update={"scheduled_at": "2026-10-04T10:00:00+03:00"})
    _, _, texts, _ = capture_intro(monkeypatch, [later, early])
    assert "СТАРТ В 09:00 МСК" in texts
    scene = r.storyboard([later, early])[0]
    texts.clear()
    assert r.render_scene(scene, NOW, 2, "UTC").size == (1080, 1920)
    assert "СТАРТ В 06:00 UTC" in texts


def test_intro_centers_event_summary_and_places_event_mark_beside_channel_emblem(monkeypatch):
    matches = [match.model_copy(update={"tournament_name": "PGL Masters Bucharest",
                                       "competition_key": "PGL Masters Bucharest"}) for match in fixtures(4)]
    monkeypatch.setattr(r.tournament_visuals, "event_logo", lambda *args: Image.new("RGBA", (100, 60), "lime"))
    calls = []
    original = r._draw_text_block
    def capture(draw, x, y, text, width, size=38, **kwargs):
        calls.append((text, x, y))
        return original(draw, x, y, text, width, size, **kwargs)
    monkeypatch.setattr(r, "_draw_text_block", capture)
    image = r.render_scene(r.storyboard(matches)[0], NOW, 4)
    assert ("МАТЧИ СЕГОДНЯ", 220, 330) in calls
    assert ("PGL MASTERS BUCHAREST", 500, 460) in calls
    assert ("4 ОКТЯБРЯ · 4 МАТЧА", 500, 535) in calls
    assert image.getpixel((820, 330)) == (0, 255, 0)
    assert image.getpixel((380, 330)) != r.CYAN
    assert image.getpixel((620, 330)) != r.CYAN
    assert not any(text == "CS2 RESULTS" for text, _, _ in calls)


def test_mixed_intro_uses_neutral_title_without_borrowing_event_logo(monkeypatch):
    first, second = fixtures(2)
    matches = [first.model_copy(update={"tournament_name": "PGL Masters Bucharest"}),
               second.model_copy(update={"tournament_name": "IEM Beijing"})]
    monkeypatch.setattr(r.tournament_visuals, "event_logo", lambda *args: Image.new("RGBA", (100, 60), "lime"))
    scene, image, texts, _ = capture_intro(monkeypatch, matches)
    assert "ТУРНИРЫ ДНЯ" in texts
    assert image.getpixel((820, 330)) != (0, 255, 0)


def test_unbranded_single_event_keeps_its_real_name_and_provider_logo(monkeypatch):
    match = fixtures(1)[0].model_copy(update={"tournament_name": "Unknown Community Cup",
        "tournament_logo_url": "preview://event"})
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: Image.new("RGBA", (100, 60), "lime"))
    scene, image, texts, _ = capture_intro(monkeypatch, [match])
    assert "UNKNOWN COMMUNITY CUP" in texts
    assert image.getpixel((820, 330)) == (0, 255, 0)


def test_logo_primary_fallback_then_initials_and_long_names(monkeypatch):
    match = fixtures(1)[0].model_copy(update={
        "team1_name": "Gaimin Gladiators Academy", "team2_name": "Natus Vincere Junior",
        "team1_logo_url": "preview://bad", "team1_logo_fallback_url": "preview://light",
        "team2_logo_url": "preview://missing", "team2_logo_fallback_url": "preview://absent"})
    urls, initials = [], []
    def fetch(url, **kwargs):
        urls.append(url)
        if url == "preview://bad":
            raise m.MediaCardError("unavailable")
        return Image.new("RGBA", (60, 60), "white") if url == "preview://light" else None
    original = m._initials
    monkeypatch.setattr(m, "fetch_team_logo", fetch)
    monkeypatch.setattr(m, "_initials", lambda name: initials.append(name) or original(name))
    _, image, texts, _ = capture_intro(monkeypatch, [match])
    assert urls == ["preview://bad", "preview://light", "preview://missing", "preview://absent"]
    assert "Natus Vincere Junior" in initials
    assert image.size == (1080, 1920)
    assert "GAIMIN GLADIATORS ACADEMY" in texts


def test_long_intro_names_fit_their_tile_without_truncation_or_logo_overlap(monkeypatch):
    matches = fixtures(4)
    matches[0] = matches[0].model_copy(update={"team1_name": "Gaimin Gladiators Academy",
        "team2_name": "Natus Vincere Junior"})
    text_boxes, logos = [], []
    original_text, original_logo = ImageDraw.ImageDraw.text, r._draw_logo
    def text(draw, xy, value, *args, **kwargs):
        if draw._image.size == r.REEL_SIZE and kwargs.get("font"):
            text_boxes.append((str(value), draw.textbbox(xy, value, font=kwargs["font"])))
        return original_text(draw, xy, value, *args, **kwargs)
    def logo(image, draw, center, diameter, name, *args, **kwargs):
        logos.append((name, center, diameter))
        return original_logo(image, draw, center, diameter, name, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(r, "_draw_logo", logo)
    r.render_scene(r.storyboard(matches)[0], NOW, 4)
    assert any(value == "ACADEMY" for value, _ in text_boxes)
    assert not any("…" in value for value, _ in text_boxes)
    _, (x, y), diameter = next(item for item in logos if item[0] == "Gaimin Gladiators Academy")
    name_boxes = [box for value, box in text_boxes if value in {"GAIMIN", "GLADIATORS", "ACADEMY"}]
    assert len(name_boxes) == 3
    assert all(box[1] > y + diameter / 2 for box in name_boxes)


def test_deadline_skips_network_and_splits_budget_between_logo_attempts(monkeypatch):
    match = fixtures(1)[0].model_copy(update={"team1_logo_url": "preview://primary",
        "team1_logo_fallback_url": "preview://fallback"})
    monkeypatch.setattr(r.time, "monotonic", lambda: 100)
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: pytest.fail("expired logo request"))
    _, _, _, logos = capture_intro(monkeypatch, [match], logo_deadline=99)
    assert all(call[0][5] is None and call[0][7] is None for call in logos)
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: None)
    logos.clear()
    r.render_scene(r.storyboard([match])[0], NOW, 1, logo_deadline=100.8)
    assert logos[0][1]["download_timeout"] == pytest.approx(.4)


def test_intro_content_is_visible_immediately_and_cached_across_frames(monkeypatch):
    matches = fixtures(4)
    calls = []
    original = r._draw_logo
    monkeypatch.setattr(r, "_draw_logo", lambda *args, **kwargs: calls.append(args[4]) or original(*args, **kwargs))
    scene = r.storyboard(matches)[0]
    frames = list(r.animated_scene_frames(scene, NOW, 4, "Europe/Moscow",
        elapsed_before=0, total_seconds=8))
    assert len(calls) == 6 and len(frames) == 18
    assert sum(duration for _, duration in frames) == pytest.approx(1.5)
    assert frames[0][0].crop((80, 620, 920, 1548)).tobytes() == frames[-1][0].crop((80, 620, 920, 1548)).tobytes()
