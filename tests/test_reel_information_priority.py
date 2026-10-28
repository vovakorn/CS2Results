"""Opening-frame visibility and tournament identity without repeated row branding."""
from datetime import datetime

from PIL import Image, ImageDraw
import pytest

from cs2bot import media_cards as m, schedule_reels as r, tournament_visuals as tv
from scripts.reel_demo_fixtures import demo_fixtures, demo_logo

NOW = datetime.fromisoformat('2026-10-28T09:00:00+03:00')


@pytest.mark.parametrize('count', [1, 4, 10, 20])
def test_schedule_is_complete_from_first_frame_and_cover(count, monkeypatch):
    monkeypatch.setattr(m, 'fetch_team_logo', demo_logo)
    matches = demo_fixtures(count)
    scenes = r.storyboard(matches)
    assert scenes[0].kind == 'matches'
    assert [match.match_id for scene in scenes for match in scene.matches] == [match.match_id for match in matches]
    visuals = r._resolve_reel_visuals(matches)
    first = scenes[0]
    expected = r.render_scene(first, NOW, count, visuals=visuals)
    frames = r.animated_scene_frames(first, NOW, count, 'Europe/Moscow',
        elapsed_before=0, total_seconds=sum(scene.duration for scene in scenes), visuals=visuals)
    crop = (95, 625, 906, 1516)
    for index, (frame, _) in enumerate(frames):
        if index in (0, 6):
            assert frame.crop(crop).tobytes() == expected.crop(crop).tobytes()
        if index == 6:
            break
    later = next((scene for scene in scenes[1:] if scene.kind == 'matches'), None)
    if later:
        hidden = r.render_scene(later, NOW, count, hide_matches=True, visuals=visuals)
        frame, _ = next(r.animated_scene_frames(later, NOW, count, 'Europe/Moscow',
            elapsed_before=first.duration, total_seconds=38, visuals=visuals))
        assert frame.crop(crop).tobytes() == hidden.crop(crop).tobytes()


@pytest.mark.parametrize('event', ['PGL Masters Bucharest', 'Unknown Community Cup', 'Открытый турнир'])
@pytest.mark.parametrize('provider_logo', [False, True])
def test_single_event_has_one_header_and_no_row_branding(event, provider_logo, monkeypatch):
    matches = demo_fixtures(4, event)
    if provider_logo:
        matches = [match.model_copy(update={'tournament_logo_url':'demo://provider'}) for match in matches]
    monkeypatch.setattr(m, 'fetch_team_logo', lambda url, **kw:
        Image.new('RGBA', (100, 60), 'lime') if url == 'demo://provider' else demo_logo(url, **kw))
    texts, watermarks = [], []
    original_text, original_watermark = ImageDraw.ImageDraw.text, tv.add_event_watermark
    def text(draw, xy, value, *args, **kwargs):
        if draw._image.size == r.REEL_SIZE:
            texts.append(str(value))
        return original_text(draw, xy, value, *args, **kwargs)
    def watermark(*args, **kwargs):
        watermarks.append(kwargs)
        return original_watermark(*args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', text)
    monkeypatch.setattr(tv, 'add_event_watermark', watermark)
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.single_event is not None
    original = visuals.single_event.logo.tobytes() if visuals.single_event.logo else None
    image = r.render_scene(r.storyboard(matches)[0], NOW, 4, visuals=visuals)
    assert texts.count(event.upper()) == 1
    assert all(row['position'] == (704, 352) for row in watermarks)
    assert len(watermarks) == (1 if visuals.shared else 0)
    if visuals.shared is None:
        assert image.getpixel((110, 330)) == r.CYAN
        if provider_logo:
            assert image.getpixel((820, 546)) == (0, 255, 0)
    if original is not None:
        assert visuals.single_event.logo.tobytes() == original


def test_mixed_day_keeps_row_identity_even_on_homogeneous_first_page(monkeypatch):
    matches = demo_fixtures(5)
    matches[-1] = matches[-1].model_copy(update={'tournament_name':'Unknown Community Cup',
        'competition_key':'Unknown Community Cup'})
    monkeypatch.setattr(m, 'fetch_team_logo', demo_logo)
    texts, watermarks = [], []
    original = ImageDraw.ImageDraw.text
    def text(draw, xy, value, *args, **kwargs):
        if draw._image.size == r.REEL_SIZE:
            texts.append(value)
        return original(draw, xy, value, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', text)
    monkeypatch.setattr(tv, 'add_event_watermark', lambda *a, **kw: watermarks.append(kw))
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.single_event is None
    r.render_scene(r.storyboard(matches)[0], NOW, 5, visuals=visuals)
    assert texts.count('PGL MASTERS BUCHAREST') == 4
    assert len(watermarks) == 4 and all(row['position'] != (704, 352) for row in watermarks)
