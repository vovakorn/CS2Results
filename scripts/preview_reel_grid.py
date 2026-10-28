"""Offline comparison: Safety Zone list versus a left-aligned 3x3 team grid."""
import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import patch
import json

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as m, schedule_reels as r
from scripts.audit_reel_accessibility import audit
from scripts.preview_reel_safety_zone import demo_fixtures, demo_logo, render


def grid_pages(fixtures):
    template = r.storyboard(fixtures)[0]
    teams = r._intro_teams(fixtures)
    pages = (len(teams) + 8) // 9
    return tuple(replace(template, page=index + 1, pages=pages, intro_teams=teams[offset:offset + 9])
                 for index, offset in enumerate(range(0, len(teams), 9)))


def render_grid(scene, now, count):
    return render(scene, now, count, grid=True)


def fixtures(count):
    sample = demo_fixtures(count)
    for index, match in enumerate(sample):
        if index % 5 == 4:
            sample[index] = match.model_copy(update={"team1_name": "paiN", "team2_name": "FlyQuest",
                "team1_logo_url": "demo://pain", "team2_logo_url": "demo://flyquest"})
    return sample


def watermark(image):
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((238, 1770, 842, 1840), radius=20, fill=(127, 31, 31))
    m._draw_text_block(draw, 540, 1805, "ДЕМО · НЕ ПУБЛИКОВАТЬ", 560, 30,
                       display=True, max_lines=1)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat("2026-10-28T09:00:00+03:00")
    selected = fixtures(5)
    pages = grid_pages(selected)
    checks = []
    with patch.object(m, "fetch_team_logo", demo_logo):
        list_image = render(r.storyboard(selected)[0], now, len(selected), preview_watermark=True)
        for index, scene in enumerate(pages, 1):
            image, records = audit(render_grid, scene, selected, now)
            assert all(row["contrast_pass"] and row["safe_bounds_pass"] for row in records)
            watermark(image).save(args.output / f"grid-{index}.png")
            checks.extend(records)
            if index == 1:
                first = image
        for count in (1, 4, 10, 20):
            sample = fixtures(count)
            if count == 20:
                sample = [match.model_copy(update={"team1_name": f"Team {index * 2 + 1}",
                    "team2_name": f"Team {index * 2 + 2}", "team1_logo_url": None, "team2_logo_url": None})
                    for index, match in enumerate(sample)]
            if count == 10:
                sample[0] = sample[0].model_copy(update={"team1_name": "Gaimin Gladiators Academy",
                    "team2_name": "Natus Vincere Junior", "team1_logo_url": None, "team2_logo_url": None})
            sample_pages = grid_pages(sample)
            assert [team for scene in sample_pages for team in scene.intro_teams] == list(r._intro_teams(sample))
            for page, scene in enumerate(sample_pages, 1):
                image, records = audit(render_grid, scene, sample, now)
                assert all(row["contrast_pass"] and row["safe_bounds_pass"] for row in records)
                watermark(image).save(args.output / f"check-{count}-{page}.png")
                checks.extend(records)
    sheet = Image.new("RGB", (1080, 1030), m.NAVY)
    draw = ImageDraw.Draw(sheet)
    for column, (label, image) in enumerate((("ALIGN LEFT · СПИСОК", list_image), ("ALIGN LEFT · СЕТКА 3X3", first))):
        m._draw_text_block(draw, column * 540 + 270, 28, label, 510, 23, display=True, max_lines=1)
        sheet.paste(image.resize((540, 960), Image.Resampling.LANCZOS), (column * 540, 60))
    sheet.save(args.output / "comparison.png")
    guide = first.copy()
    ImageDraw.Draw(guide).rectangle((80, 250, 920, 1570), outline=(22, 199, 255), width=2)
    guide.save(args.output / "grid-guides.png")
    summary = {"grid": [3, 3], "teams_per_page": 9, "demo_teams": 10, "demo_pages": len(pages),
        "checked_match_counts": [1, 4, 10, 20], "maximum_distinct_teams_checked": 40,
        "maximum_grid_pages_checked": 5,
        "text_runs_checked": len(checks), "minimum_contrast": min(row["minimum_ratio"] for row in checks),
        "safe_bounds_failures": 0, "runtime_changed": False, "selected": False, "published": False}
    (args.output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
