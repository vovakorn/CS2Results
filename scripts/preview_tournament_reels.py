"""Offline, watermarked Reel themes; example profiles never enter the live registry."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized
from cs2bot.tournament_preview import load_profiles
from preview_schedule_reels import _synthetic_logo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counts", type=int, nargs="+", default=[4])
    parser.add_argument("--audio-style", choices=("esports", "minimal"), default="minimal")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles(ROOT / "data/tournament_preview_profiles.json")
    profiles += load_profiles(ROOT / "data/tournament_preview_examples.json")
    titles = ["ESL Pro League Season 24", "BLAST Rivals Hong Kong 2026", "PGL Masters Bucharest 2026"]
    names = [("Team Spirit", "FUT Esports"), ("NAVI", "MOUZ"), ("G2 Esports", "FaZe Clan"),
             ("Gaimin Gladiators Academy", "Natus Vincere Junior")]
    now = datetime(2026, 10, 3, 9, tzinfo=ZoneInfo("Europe/Moscow"))
    profile_by_match = {}
    old_identity, old_loader = r.event_for_match, m.fetch_team_logo
    r.event_for_match = lambda match, path: profile_by_match.get(match.match_id)
    m.fetch_team_logo = _synthetic_logo
    try:
        def fixtures(count, selected):
            result = []
            for index in range(count):
                choice = selected[index % len(selected)]
                match_id = f"demo-{choice}-{index}"
                profile_by_match[match_id] = profiles[choice]
                team1, team2 = names[index % len(names)]
                result.append(UpcomingMatchNormalized(match_id=match_id,
                    tournament_name=titles[choice], competition_key=titles[choice],
                    team1_name=team1, team2_name=team2, best_of=3,
                    team1_logo_url="preview://light" if index % 3 == 0 else None,
                    team2_logo_url="preview://dark" if index % 3 == 0 else None,
                    scheduled_at=(now.replace(hour=10) + timedelta(minutes=30 * index)).isoformat()))
            return result

        sheets = []
        for choices, label in [([0], "epl"), ([1], "blast"), ([2], "pgl"), ([0, 1, 2], "mixed")]:
            for count in args.counts:
                matches = fixtures(count, choices)
                scenes = r.storyboard(matches)
                tiles = []
                for index, scene in enumerate(scenes):
                    image = r.render_scene(scene, now, count, preview_watermark=True)
                    image.save(args.output / f"{label}-{count}-{index}.png")
                    tile = image.resize((360, 640), Image.Resampling.LANCZOS)
                    tile.save(args.output / f"{label}-{count}-{index}-360.png")
                    if index < 2:
                        tiles.append(tile)
                if count == 4:
                    sheet = Image.new("RGB", (720, 680), m.NAVY)
                    draw = ImageDraw.Draw(sheet)
                    draw.text((12, 10), label.upper(), font=m._font(22), fill=m.WHITE)
                    for column, tile in enumerate(tiles):
                        sheet.paste(tile, (column * 360, 40))
                    sheet.save(args.output / f"{label}-comparison.png")
                    if label != "mixed":
                        sheets.append(tiles[1])
                output = args.output / f"{label}-{count}.mp4"
                output.write_bytes(r.render_schedule_reel(matches, now,
                    preview_watermark=True, audio_style=args.audio_style))
                print(f"{output.name}: {output.stat().st_size} bytes", flush=True)
        if sheets:
            comparison = Image.new("RGB", (1080, 640), m.NAVY)
            for index, tile in enumerate(sheets):
                comparison.paste(tile, (index * 360, 0))
            comparison.save(args.output / "themes.png")
    finally:
        r.event_for_match, m.fetch_team_logo = old_identity, old_loader


if __name__ == "__main__":
    main()
