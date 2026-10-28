"""Measure rendered match columns and the centred hero group."""
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
import pytest

from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized


@pytest.mark.parametrize('kind', ['row', 'hero', 'reel'])
@pytest.mark.parametrize('names', [('TheMongolz', 'Nemiga'),
    ('Gaimin Gladiators Academy', 'Natus Vincere Junior'),
    ('Длинное название команды', 'Team Spirit')])
@pytest.mark.parametrize('footer', [False, True])
def test_match_groups_have_shared_axes_and_no_text_collisions(monkeypatch, kind, names, footer):
    match = UpcomingMatchNormalized(match_id='alignment', tournament_name='PGL Masters Bucharest',
        team1_name=names[0], team2_name=names[1], best_of=3,
        scheduled_at='2026-10-28T14:00:00+03:00')
    image = Image.new('RGB', r.REEL_SIZE if kind == 'reel' else (1080, 1350), m.NAVY)
    draw = ImageDraw.Draw(image)
    names_upper = [name.upper() for name in names]
    blocks, logos, times, active = [], [], [], []
    original_block, original_text, original_point = m._draw_text_block, ImageDraw.ImageDraw.text, m._centered_text_on_point

    def block(draw, x, y, text, width, size=38, **kwargs):
        record = {'text': text, 'x': x, 'y': y, 'size': size, 'boxes': [], 'lines': []}
        if text in names_upper:
            blocks.append(record)
            active.append(record)
        try:
            return original_block(draw, x, y, text, width, size, **kwargs)
        finally:
            if text in names_upper:
                active.pop()

    def text(draw, xy, value, *args, **kwargs):
        if active and kwargs.get('font'):
            active[-1]['boxes'].append(draw.textbbox(xy, value, font=kwargs['font']))
            active[-1]['lines'].append(value)
        return original_text(draw, xy, value, *args, **kwargs)

    def point(draw, x, y, value, font, fill):
        if value == '14:00':
            times.append((x, y, draw.textbbox((0, 0), value, font=font)))
        return original_point(draw, x, y, value, font, fill)

    def logo(image, draw, center, diameter, *args, **kwargs):
        logos.append((center, diameter))

    monkeypatch.setattr(m, '_draw_text_block', block)
    monkeypatch.setattr(r, '_draw_text_block', block)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', text)
    monkeypatch.setattr(m, '_centered_text_on_point', point)
    monkeypatch.setattr(m, '_draw_logo', logo)
    monkeypatch.setattr(r, '_draw_logo', logo)
    box = (60, 470, 1020, 960) if kind == 'hero' else (60, 400, 1020, 590)
    if kind == 'reel':
        r._match_card(image, match, 700, ZoneInfo('Europe/Moscow'), None)
    else:
        renderer = m._draw_fixture_hero if kind == 'hero' else m._draw_fixture_row
        renderer(image, draw, match, box, time_label='14:00', show_format=footer,
                 event='PGL Masters Bucharest' if footer else None)
    assert len(logos) == len(blocks) == 2 and len(times) == 1
    assert blocks[0]['size'] == blocks[1]['size']
    assert blocks[0]['y'] == blocks[1]['y']
    assert logos[0][0][1] == logos[1][0][1] == times[0][1]
    axis = 500 if kind == 'reel' else 540
    assert times[0][0] == axis
    assert logos[0][0][0] + logos[1][0][0] == 2 * axis
    for record in blocks:
        assert ' '.join(record['lines']) == record['text']
        assert all(0 <= b[0] < b[2] <= image.width and 0 <= b[1] < b[3] <= image.height for b in record['boxes'])
    if kind == 'hero':
        top = logos[0][0][1] - logos[0][1] / 2
        bottom = max(b[3] for record in blocks for b in record['boxes'])
        assert abs((top + bottom) / 2 - (box[1] + box[3] - (45 if footer else 0)) / 2) <= 1
        assert min(b[1] for record in blocks for b in record['boxes']) >= logos[0][0][1] + logos[0][1] / 2 + 22
    else:
        assert blocks[0]['y'] == times[0][1]
        time_width = times[0][2][2] - times[0][2][0]
        assert max(b[2] for b in blocks[0]['boxes']) < axis - time_width / 2
        assert min(b[0] for b in blocks[1]['boxes']) > axis + time_width / 2
        if kind == 'row' and not footer:
            assert times[0][1] == (box[1] + box[3]) / 2
