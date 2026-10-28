"""Shared offline data and bundled logos for the selected Reel template."""
from pathlib import Path

from PIL import Image

from cs2bot.match_sources.models import UpcomingMatchNormalized

ROOT = Path(__file__).resolve().parents[1]


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
