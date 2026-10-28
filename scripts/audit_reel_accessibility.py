"""Audit text contrast on actual Reel underlays; this is not full WCAG conformance."""
import argparse
from datetime import datetime
from functools import lru_cache
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

from PIL import Image, ImageColor, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as m, schedule_reels as r
from scripts.reel_demo_fixtures import demo_fixtures, demo_logo


@lru_cache(maxsize=100000)
def luminance(rgb):
    values = [v / 255 for v in rgb]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))


def contrast(foreground, background):
    a, b = sorted((luminance(tuple(foreground)), luminance(tuple(background))))
    return (b + .05) / (a + .05)


def audit(render, scene, fixtures, now):
    records = []
    original = ImageDraw.ImageDraw.text
    def text(draw, xy, value, *args, **kwargs):
        if draw._image.size == r.REEL_SIZE and draw._image.mode in {"RGB", "RGBA"} and value:
            font = kwargs.get("font")
            fill = kwargs.get("fill", args[0] if args else None)
            if font is not None and fill is not None:
                foreground = ImageColor.getrgb(fill) if isinstance(fill, str) else tuple(fill[:3])
                box = draw.textbbox(xy, value, font=font)
                left, top, right, bottom = math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3])
                if right > left and bottom > top:
                    mask = Image.new("L", (right - left, bottom - top))
                    original(ImageDraw.Draw(mask), (xy[0] - left, xy[1] - top), value, font=font, fill=255)
                    background = draw._image.crop((left, top, right, bottom)).convert("RGB")
                    colors = {rgb for rgb, coverage in zip(background.get_flattened_data(), mask.get_flattened_data())
                              if coverage >= 250}
                    if colors:
                        minimum = min(contrast(foreground, rgb) for rgb in colors)
                        demo = "ДЕМО" in str(value)
                        records.append({"text": str(value), "font_px": font.size, "bbox": list(box),
                            "foreground": list(foreground), "minimum_ratio": minimum,
                            "threshold": 4.5, "contrast_pass": minimum >= 4.5,
                            "safe_bounds_pass": demo or (left >= 80 and right <= 920 and top >= 250 and bottom <= 1570),
                            "preview_only": demo})
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(ImageDraw.ImageDraw, "text", text):
        image = render(scene, now, len(fixtures))
    return image, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat("2026-10-28T09:00:00+03:00")
    events = ["PGL Masters Bucharest", "IEM Beijing 2026", "BLAST Rivals Hong Kong 2026",
              "PGL Major Singapore 2026", "ESL Pro League Season 24", "Unknown Community Cup", "Mixed day"]
    results = []
    with patch.object(m, "fetch_team_logo", demo_logo):
        for event in events:
            for count in (1, 4, 10):
                fixtures = demo_fixtures(count, event)
                if event == "Mixed day":
                    fixtures = [match.model_copy(update={"tournament_name": "PGL Masters Bucharest" if index % 2 == 0 else "IEM Beijing 2026",
                        "competition_key": "PGL Masters Bucharest" if index % 2 == 0 else "IEM Beijing 2026"})
                        for index, match in enumerate(fixtures)]
                if count == 10:
                    fixtures[0] = fixtures[0].model_copy(update={"team1_name": "Gaimin Gladiators Academy",
                        "team2_name": "Natus Vincere Junior", "team1_logo_url": None, "team2_logo_url": None})
                for scene in r.storyboard(fixtures):
                    _, records = audit(r.render_scene, scene, fixtures, now)
                    results.append({"event": event, "count": count, "scene": scene.kind,
                        "page": scene.page, "variant": "standard", "text": records})
    records = [row for case in results for row in case["text"] if not row["preview_only"]]
    summary = {"cases": len(results), "text_runs": len(records),
               "minimum_ratio": min(row["minimum_ratio"] for row in records),
               "contrast_failures": sum(not row["contrast_pass"] for row in records),
               "bounds_failures": sum(not row["safe_bounds_pass"] for row in records),
               "scope": "1.4.3 sampled text; platform safe bounds; not whole WCAG AA conformance",
               "large_text_discount": False, "published": False}
    (args.output / "wcag-evidence.json").write_text(json.dumps({"summary": summary, "cases": results},
        ensure_ascii=False, indent=2) + "\n")
    failures = [dict(event=case["event"], variant=case["variant"], scene=case["scene"], **row)
                for case in results for row in case["text"] if not row["contrast_pass"] or not row["safe_bounds_pass"]]
    (args.output / "wcag-failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    for item in failures[:8]:
        print(json.dumps({key: item[key] for key in ("event", "variant", "scene", "text", "minimum_ratio")}, ensure_ascii=False))
    if summary["contrast_failures"] or summary["bounds_failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
