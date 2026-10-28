"""Offline alternative intro: left-aligned content and no outer frame."""
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import argparse
import sys
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized

LEFT, RIGHT = 96, 904


def heading(image, draw, local_now, count, visual, accent):
    draw.rounded_rectangle((LEFT, 307, 345, 353), radius=10, fill=accent)
    r._draw_text_block(draw, 220, 330, "МАТЧИ СЕГОДНЯ", 225, 22, min_size=22,
                       display=True, fill=m.NAVY, max_lines=1)
    if visual and visual.logo is not None:
        mark = ImageOps.contain(visual.logo, (128, 86))
        image.alpha_composite(mark, (820 - mark.width // 2, 330 - mark.height // 2))
    title = visual.name.upper() if visual else "ТУРНИРЫ ДНЯ"
    r._draw_text_block(draw, LEFT, 460, title, 808, 38, min_size=30,
                       display=m._uses_cyrillic(title), alignment="left", max_lines=2)
    r._draw_text_block(draw, LEFT, 535,
                       f"{local_now.day} {r.MONTHS[local_now.month]} · {count} {r._match_noun(count).upper()}",
                       808, 27, min_size=24, display=True, fill=m.MUTED, alignment="left", max_lines=1)


def teams(image, draw, fixtures, count, tz, accent, logo_deadline, *, branding=None,
          teams=None, page=1, pages=1, grid=False):
    for y, text, color in ((658, "ТВОЯ КОМАНДА", m.WHITE), (734, "ИГРАЕТ СЕГОДНЯ?", r._text_accent(accent))):
        r._draw_text_block(draw, LEFT, y, text, 808, 68, min_size=64,
                           display=True, alignment="left", max_lines=1, fill=color)
    shown = teams if teams is not None else r._intro_teams(fixtures)[:9 if grid else 6]
    panel = r.panel_color(branding, m.PANEL)
    for index, team in enumerate(shown):
        if grid:
            left, top = LEFT + index % 3 * 277, 814 + index // 3 * 186
            right, bottom = left + 253, top + 174
            logo_center, diameter = (left + 48, top + 44), 64
            text_x, text_y, text_width, size, lines = left + 14, top + 127, 225, 32, 3
        else:
            left, right, top, bottom = LEFT, RIGHT, 814 + index * 94, 896 + index * 94
            logo_center, diameter = (140, (top + bottom) // 2), 56
            text_x, text_y, text_width, size, lines = 188, (top + bottom) // 2, 692, 40, 2
        draw.rounded_rectangle((left, top, right, bottom), radius=16,
                               fill=(*panel, 255), outline=(*accent, 100), width=1)
        r._draw_logo(image, draw, logo_center, diameter, team.name,
                      team.logo_url, accent, team.fallback_url, subtle=True, download_timeout=.75)
        r._draw_text_block(draw, text_x, text_y, team.name.upper(), text_width, size,
                           min_size=30 if grid else 32, max_lines=lines,
                           display=m._uses_cyrillic(team.name), alignment="left")
    if pages > 1:
        r._draw_text_block(draw, LEFT, 1416, f"КОМАНДЫ {page} / {pages}", 808, 30,
                           display=True, alignment="left", fill=m.MUTED, max_lines=1)
    if fixtures:
        first = min(fixtures, key=lambda match: datetime.fromisoformat(match.scheduled_at.replace("Z", "+00:00")))
        zone = "МСК" if tz.key == "Europe/Moscow" else tz.key
        r._draw_text_block(draw, LEFT, 1468, f"СТАРТ В {m._schedule_time(first, tz)} {zone}",
                           808, 36, min_size=32, alignment="left", display=True, fill=r._text_accent(accent), max_lines=1)
    r._draw_text_block(draw, LEFT, 1520, "РАСПИСАНИЕ — ДАЛЬШЕ", 808, 26,
                       alignment="left", display=True, fill=m.MUTED, max_lines=1)


def render(scene, now, count, *, grid=False, **kwargs):
    original = ImageDraw.ImageDraw.rounded_rectangle
    def rectangle(draw, xy, *args, **kw):
        if draw._image.size == r.REEL_SIZE and tuple(xy) == (72, 230, 928, 1590):
            return
        return original(draw, xy, *args, **kw)
    with ExitStack() as stack:
        stack.enter_context(patch.object(r, "_intro_heading", heading))
        def team_layout(*args, **kw):
            return teams(*args, **kw, grid=grid)
        stack.enter_context(patch.object(r, "_draw_day_intro", team_layout))
        stack.enter_context(patch.object(ImageDraw.ImageDraw, "rounded_rectangle", rectangle))
        return r.render_scene(scene, now, count, **kwargs)


def demo_fixtures(count=4, event="PGL Masters Bucharest"):
    pairs = [("TheMongolz", "Nemiga", "the-mongolz", "nemiga"),
             ("Liquid", "3DMAX", "liquid", "3dmax"),
             ("HEROIC", "BIG", "heroic", "big"),
             ("GamerLegion", "TYLOO", "gamerlegion", "tyloo")]
    return [UpcomingMatchNormalized(match_id=f"demo-{i}", tournament_name=event, competition_key=event,
        team1_name=pairs[i % len(pairs)][0], team2_name=pairs[i % len(pairs)][1], best_of=3,
        team1_logo_url=f"demo://{pairs[i % len(pairs)][2]}", team2_logo_url=f"demo://{pairs[i % len(pairs)][3]}",
        scheduled_at=f"2026-10-28T{10 + i // 2:02}:{30 * (i % 2):02}:00+03:00") for i in range(count)]


def demo_logo(url, **kwargs):
    if url and url.startswith("demo://"):
        with Image.open(ROOT / "video/vrs-major-race/assets/logos" / f"{url[7:]}.png") as source:
            return source.convert("RGBA")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat("2026-10-28T09:00:00+03:00")
    fixtures = demo_fixtures()
    intro = [scene for scene in r.storyboard(fixtures) if scene.kind == "intro"]
    with patch.object(m, "fetch_team_logo", demo_logo):
        for index, scene in enumerate(intro, 1):
            standard = r.render_scene(scene, now, len(fixtures), preview_watermark=True)
            safety = render(scene, now, len(fixtures), preview_watermark=True)
            standard.save(args.output / f"standard-{index}.png")
            safety.save(args.output / f"safety-{index}.png")
            if index == 1:
                sheet = Image.new("RGB", (1080, 1030), m.NAVY)
                draw = ImageDraw.Draw(sheet)
                for col, (label, image) in enumerate((("ОСНОВНОЙ", standard), ("SAFETY ZONE · ALIGN LEFT", safety))):
                    m._draw_text_block(draw, col * 540 + 270, 28, label, 510, 23, display=True, max_lines=1)
                    sheet.paste(image.resize((540, 960), Image.Resampling.LANCZOS), (col * 540, 60))
                sheet.save(args.output / "comparison.png")
                guide = safety.copy()
                ImageDraw.Draw(guide).rectangle((80, 250, 920, 1570), outline=(22, 199, 255), width=2)
                guide.save(args.output / "safety-guides.png")
    print(args.output)


if __name__ == "__main__":
    main()
