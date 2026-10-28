"""Compare current panels with a panel-free layout, offline and without runtime changes."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import sys
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as media, schedule_reels as reels, tournament_theme_patterns as themes
from cs2bot.match_sources.models import UpcomingMatchNormalized


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat("2026-10-28T09:00:00+03:00")
    pairs = [("TheMongolz", "Nemiga", "the-mongolz", "nemiga"),
             ("Liquid", "3DMAX", "liquid", "3dmax"),
             ("HEROIC", "BIG", "heroic", "big"),
             ("GamerLegion", "TYLOO", "gamerlegion", "tyloo")]
    fixtures = [UpcomingMatchNormalized(match_id=f"demo-{index}",
        tournament_name="PGL Masters Bucharest", competition_key="PGL Masters Bucharest",
        team1_name=left, team2_name=right, team1_logo_url=f"demo://{left_slug}",
        team2_logo_url=f"demo://{right_slug}", best_of=3,
        scheduled_at=(now.replace(hour=14) + timedelta(hours=index)).isoformat())
        for index, (left, right, left_slug, right_slug) in enumerate(pairs)]

    def logo(url, **kwargs):
        if url and url.startswith("demo://"):
            with Image.open(ROOT / "video/vrs-major-race/assets/logos" / f"{url[7:]}.png") as image:
                return image.convert("RGBA")
        return None

    old_footer = media._card_footer

    def footer(draw, *args, **kwargs):
        old_footer(draw, *args, **kwargs)
        media._draw_text_block(draw, 540, draw._image.height - 30, "МАКЕТ · НЕ ДЛЯ ПУБЛИКАЦИИ",
                              800, 16, display=True, fill=media.AMBER, max_lines=1)

    original_rectangle = ImageDraw.ImageDraw.rounded_rectangle
    original_text = ImageDraw.ImageDraw.text
    images, checks = {}, []

    def variant(name, render, boxes):
        text_ops = []

        def record_text(draw, xy, value, *args, **kwargs):
            text_ops.append((draw._image.size, tuple(xy), str(value),
                             draw.textbbox(xy, value, font=kwargs.get("font"))))
            return original_text(draw, xy, value, *args, **kwargs)

        def no_reel_panel(draw, xy, *args, **kwargs):
            if draw._image.size == reels.REEL_SIZE and tuple(xy) in boxes:
                return
            return original_rectangle(draw, xy, *args, **kwargs)

        for mode in ("panels", "open"):
            text_ops.clear()
            with ExitStack() as stack:
                stack.enter_context(patch.object(ImageDraw.ImageDraw, "text", record_text))
                if mode == "open":
                    stack.enter_context(patch.object(themes, "draw_result_panel", lambda *a, **kw: None))
                    stack.enter_context(patch.object(ImageDraw.ImageDraw, "rounded_rectangle", no_reel_panel))
                result = render()
                image = Image.open(BytesIO(result)).convert("RGB") if isinstance(result, bytes) else result.convert("RGB")
            images[name, mode] = image
            image.save(args.output / f"{name}-{mode}.png")
            if mode == "panels":
                previous_text = list(text_ops)
            else:
                assert previous_text == text_ops, (name, "Text or position changed")
        difference = ImageChops.difference(images[name, "panels"], images[name, "open"])
        assert difference.getbbox(), (name, "No panels removed")
        allowed = Image.new("L", difference.size)
        draw = ImageDraw.Draw(allowed)
        for box in boxes:
            draw.rectangle(box, fill=255)
        assert ImageChops.multiply(difference, ImageChops.invert(allowed).convert("RGB")).getbbox() is None
        checks.append({"layout": name, "size": list(difference.size),
                       "text_unchanged": True, "changes_only_inside_panels": True})

    with patch.object(media, "fetch_team_logo", logo), patch.object(media, "_card_footer", footer):
        for card_format in ("square", "portrait"):
            context_box = (60, 410, 1020, 820) if card_format == "square" else (60, 475, 1020, 980)
            variant(f"context-{card_format}", lambda: media.render_schedule_context_covers(
                fixtures[:3], now, card_format=card_format)[0], [context_box])
            variant(f"schedule-{card_format}", lambda: media.render_schedule_card(
                fixtures, now, "Europe/Moscow", card_format=card_format),
                media._fixture_page_boxes(4, card_height=media.static_card_size(card_format)[1]))
        scenes = reels.storyboard(fixtures)
        intro_boxes = reels._intro_team_boxes(min(reels.MAX_INTRO_TEAMS, len(reels._intro_teams(fixtures))))
        variant("reel-intro", lambda: reels.render_scene(scenes[0], now, 4, preview_watermark=True), intro_boxes)
        start = 626 + (888 - (4 * 210 + 3 * 16)) // 2
        match_boxes = [(96, start + index * 226, 904, start + index * 226 + 210) for index in range(4)]
        match_scene = next(scene for scene in scenes if scene.kind == "matches")
        variant("reel-matches", lambda: reels.render_scene(match_scene, now, 4, preview_watermark=True), match_boxes)

    def comparison(filename, names, tile_width):
        tile_height = max(round(images[name, "panels"].height * tile_width /
                                images[name, "panels"].width) for name in names)
        group_width = len(names) * tile_width
        sheet = Image.new("RGB", (group_width * 2, tile_height + 92), media.NAVY)
        draw = ImageDraw.Draw(sheet)
        for group, (mode, label) in enumerate((("panels", "А · С ПОДЛОЖКОЙ"), ("open", "Б · БЕЗ ПОДЛОЖКИ"))):
            media._draw_text_block(draw, group * group_width + group_width // 2, 25, label,
                                  group_width - 30, 27, display=True, max_lines=1)
            for index, name in enumerate(names):
                image = images[name, mode]
                resized = image.resize((tile_width, round(image.height * tile_width / image.width)),
                                       Image.Resampling.LANCZOS)
                sheet.paste(resized, (group * group_width + index * tile_width, 60))
        sheet.save(args.output / filename)

    comparison("compare-context.png", ["context-square"], 540)
    comparison("compare-schedule.png", ["schedule-portrait"], 480)
    comparison("compare-reel.png", ["reel-intro", "reel-matches"], 270)
    manifest = {"variants": ["panels", "open"], "renders": len(images), "checks": checks,
                "network": "disabled", "runtime_changed": False, "selected": None, "published": False}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"renders": len(images), "output": str(args.output), "checks": len(checks)}))


if __name__ == "__main__":
    main()
