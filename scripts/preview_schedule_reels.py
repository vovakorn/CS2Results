#!/usr/bin/env python3
"""Generate clearly watermarked local Reel previews from synthetic fixtures."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cs2bot import media_cards  # noqa: E402
from cs2bot.match_sources.models import UpcomingMatchNormalized  # noqa: E402
from cs2bot.schedule_reels import render_schedule_reel, render_scene, storyboard  # noqa: E402


TEAM_NAMES = (
    "Team Spirit", "Team Vitality", "MOUZ", "Natus Vincere", "G2 Esports",
    "FaZe Clan", "The MongolZ", "FURIA", "Astralis", "Team Liquid",
    "Virtus.pro", "Complexity", "3DMAX", "HEROIC", "ENCE", "Aurora",
    "Eternal Fire", "BIG", "GamerLegion", "Falcons",
    "Очень Длинное Название Команды Для Проверки Размещения", "OG",
)


def _synthetic_logo(url: str, **kwargs: object) -> Image.Image | None:
    if not url.startswith("preview://"):
        return None
    light = url.endswith("light")
    image = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 18, 222, 222), fill=(247, 247, 247, 255) if light else (8, 14, 23, 255))
    draw.polygon(((120, 45), (190, 176), (50, 176)), fill=(22, 199, 255, 255) if light else (255, 159, 28, 255))
    return image


def _fixtures(count: int, now: datetime) -> list[UpcomingMatchNormalized]:
    start = now.replace(hour=10, minute=0, second=0, microsecond=0)
    fixtures: list[UpcomingMatchNormalized] = []
    for index in range(count):
        fixtures.append(UpcomingMatchNormalized(
            match_id=f"demo-{index + 1}",
            tournament_name=(
                "BLAST Open — Проверка длинного названия турнира"
                if index == 2 else "IEM DEMO — ПРИМЕР"
            ),
            team1_name=TEAM_NAMES[(index * 2) % len(TEAM_NAMES)],
            team2_name=TEAM_NAMES[(index * 2 + 1) % len(TEAM_NAMES)],
            team1_logo_url="preview://light" if index % 3 == 0 else None,
            team2_logo_url="preview://dark" if index % 4 == 0 else None,
            scheduled_at=(start + timedelta(minutes=35 * index)).isoformat(),
            best_of=3,
            is_featured=True,
        ))
    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/reel-previews"))
    parser.add_argument("--counts", type=int, nargs="+", default=[1, 4, 10, 20])
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    original_logo_loader = media_cards.fetch_team_logo
    media_cards.fetch_team_logo = _synthetic_logo
    try:
        for count in args.counts:
            fixtures = _fixtures(count, now)
            scenes = storyboard(fixtures)
            still = render_scene(scenes[1], now, count, preview_watermark=True)
            still_path = args.output_dir / f"schedule-reel-{count}-scene.png"
            still.save(still_path)
            video_path = args.output_dir / f"schedule-reel-{count}.mp4"
            video_path.write_bytes(render_schedule_reel(fixtures, now, preview_watermark=True))
            print(f"{count}: {video_path.resolve()} ({video_path.stat().st_size} bytes)")
    finally:
        media_cards.fetch_team_logo = original_logo_loader


if __name__ == "__main__":
    main()
