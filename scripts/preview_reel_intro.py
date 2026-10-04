"""Offline, watermarked previews of the team-day Reel opener."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import SourceReferences, UpcomingMatchNormalized
from cs2bot.tournament_preview import load_profiles
from preview_schedule_reels import _synthetic_logo

NAMES = ("Team Spirit", "Team Vitality", "NAVI", "MOUZ", "FaZe Clan", "G2 Esports",
         "Aurora", "The MongolZ", "Gaimin Gladiators Academy", "Natus Vincere Junior",
         "FURIA", "Falcons", "Team Liquid", "Astralis", "HEROIC", "Virtus.pro",
         "GamerLegion", "Complexity", "ENCE", "BIG", "3DMAX", "OG")
NOW = datetime(2026, 10, 4, 9, tzinfo=ZoneInfo("Europe/Moscow"))


def fixtures(count, *, missing=False, long=False, mixed=False):
    profile = load_profiles(ROOT / "data/tournament_preview_profiles.json")[0]
    matches = []
    for index in range(count):
        first, second = (NAMES[(index * 2) % len(NAMES)], NAMES[(index * 2 + 1) % len(NAMES)])
        if long and index == 0:
            first, second = "Gaimin Gladiators Academy", "Natus Vincere Junior"
        matches.append(UpcomingMatchNormalized(match_id=f"demo-{index:02}",
            tournament_name="ESL Pro League Season 24" if not mixed or index % 2 == 0 else "Другой турнир · ДЕМО",
            source_refs=SourceReferences(serie_id=str(profile.pandascore_serie_id)) if not mixed or index % 2 == 0 else None,
            team1_name=first, team2_name=second,
            team1_logo_url=None if missing or index % 3 == 2 else "preview://light",
            team2_logo_url=None if missing or index % 3 == 2 else "preview://dark",
            # Each first pair of matches shares a start; no arbitrary hero.
            scheduled_at=(NOW.replace(hour=10) + timedelta(minutes=(index // 2) * 60)).isoformat(),
            best_of=3))
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--encode-counts", type=int, nargs="*", default=[4])
    parser.add_argument("--audio-style", choices=("esports", "minimal"), default="minimal")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    old_loader = m.fetch_team_logo
    m.fetch_team_logo = _synthetic_logo
    try:
        tiles = []
        for count in (1, 4, 10, 20):
            image = r.render_scene(r.storyboard(fixtures(count))[0], NOW, count, preview_watermark=True)
            image.save(args.output / f"intro-{count}.png")
            tile = image.resize((360, 640), Image.Resampling.LANCZOS)
            tile.save(args.output / f"intro-{count}-360.png")
            if count in (1, 4, 10):
                tiles.append(tile)
        sheet = Image.new("RGB", (1080, 640), m.NAVY)
        for index, tile in enumerate(tiles):
            sheet.paste(tile, (360 * index, 0))
        sheet.save(args.output / "intro-comparison.png")
        for name, kwargs in (("missing", {"missing": True}), ("long", {"long": True}), ("mixed", {"mixed": True})):
            image = r.render_scene(r.storyboard(fixtures(4, **kwargs))[0], NOW, 4, preview_watermark=True)
            image.save(args.output / f"intro-{name}.png")
        for count in args.encode_counts:
            data = r.render_schedule_reel(fixtures(count), NOW,
                preview_watermark=True, audio_style=args.audio_style)
            path = args.output / f"reel-{count}.mp4"
            path.write_bytes(data)
            print(f"{path.name}: {len(data)} bytes", flush=True)
    finally:
        m.fetch_team_logo = old_loader


if __name__ == "__main__":
    main()
