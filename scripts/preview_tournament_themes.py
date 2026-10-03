#!/usr/bin/env python3
"""Render the approved visual atlas with marked fictional passports; no publishing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cs2bot import media_cards
from cs2bot.tournament_branding import load_theme_registry
from cs2bot.tournament_preview import TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    research = json.loads((ROOT / "docs/research/tournament-branding-2026-10-03.json").read_text())
    titles = {theme["key"]: theme["title"] for theme in research["themes"]}
    fixture = json.loads((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
    covers = []
    for theme in load_theme_registry().themes:
        if theme.key not in titles:
            continue
        for count in (1, 4, 10):
            data = dict(fixture, name=titles[theme.key], participant_count=count,
                        teams=[dict(team_id=f"demo-{i}", name=f"Demo Team {i + 1}") for i in range(count)],
                        branding={"theme_key": theme.key})
            preview = TournamentPreview.model_validate(data)
            directory = args.output / theme.key / str(count)
            directory.mkdir(parents=True, exist_ok=True)
            for i, card in enumerate(render_preview_cards(preview, demo=True), 1):
                (directory / f"{i:02d}.png").write_bytes(card)
            if count == 4:
                covers.append((theme.key, Image.open(directory / "01.png").copy()))
    for keys, filename, columns in [
        ([key for key, _ in covers], "all-full-covers.jpg", 4),
        (["thunderpick", "fissure", "cac"], "approved-three-full-covers.jpg", 3),
    ]:
        rows = (len(keys) + columns - 1) // columns
        board = Image.new("RGB", (columns * 420, rows * 455 + 70), media_cards.NAVY)
        draw = ImageDraw.Draw(board)
        draw.text((20, 18), "CS2 RESULTS / ДЕМОНСТРАЦИОННЫЕ ПРЕВЬЮ", font=media_cards._font(25, display=True), fill="white")
        by_key = dict(covers)
        for i, key in enumerate(keys):
            x, y = (i % columns) * 420 + 10, (i // columns) * 455 + 70
            board.paste(by_key[key].resize((400, 400), Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 10, y + 409), key.upper(), font=media_cards._font(23), fill=media_cards.MUTED)
        board.save(args.output / filename, quality=94)
    print(json.dumps({"themes": len(covers), "participant_variants": [1, 4, 10], "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
