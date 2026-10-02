"""Render fictional tournament series locally using the announcement brand profiles."""
import argparse
from io import BytesIO
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from cs2bot import media_cards
from cs2bot.match_sources.models import MatchNormalized, MapResult, TournamentPlacement
from cs2bot.tournament_preview import load_profiles, TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cover


def save_demo(data, path):
    image = Image.open(BytesIO(data)).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 986, 1044, 1041), fill=media_cards.NAVY)
    media_cards._draw_text_block(draw, 540, 1015, "МАКЕТ · ВЫМЫШЛЕННЫЙ РЕЗУЛЬТАТ",
                                  940, 22, display=True, fill=media_cards.AMBER, max_lines=1)
    image.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles(ROOT / "data/tournament_preview_examples.json")
    profiles += load_profiles(ROOT / "data/tournament_preview_profiles.json")
    names = ("Mirage", "Nuke", "Ancient", "Dust II", "Inferno")
    for profile, title in zip(profiles, (
        "BLAST Rivals Hong Kong 2026", "PGL Masters Bucharest 2026", "ESL Pro League Season 24"
    ), strict=True):
        preview = TournamentPreview.model_validate_json(
            (ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
        preview = preview.model_copy(update={"name": title, "branding": profile.branding,
            "start": min(stage.start for stage in profile.stages),
            "end": max(stage.end for stage in profile.stages), "stages": profile.stages,
            "first_match_at": None})
        (args.output / f"{profile.key}-announcement.png").write_bytes(render_preview_cover(preview, demo=True))
        teams = ["Team Spirit", "FUT Esports", "Vitality", "MOUZ", "NAVI", "FaZe Clan",
                 "FURIA", "G2 Esports", "Team Liquid", "Astralis", "The MongolZ", "3DMAX",
                 "Virtus.pro", "BIG", "GamerLegion", "paiN Gaming"]
        placements = [TournamentPlacement(placement=str(i + 1), team_name=name,
            prize_usd=300000 if i == 0 else 150000 if i == 1 else 100000 if i == 2
            else 90000 if i == 3 else 30000)
            for i, name in enumerate(teams)]
        for page, data in enumerate(media_cards.render_tournament_standings_cards(
            title, placements, branding=profile.branding), 1):
            save_demo(data, args.output / f"{profile.key}-standings-{page}.png")
        for count in (3, 4, 5):
            lost_maps = {3: set(), 4: {2}, 5: {1, 3}}[count]
            maps = [MapResult(name=name, score1=10 if i in lost_maps else 13,
                              score2=13 if i in lost_maps else 9)
                    for i, name in enumerate(names[:count])]
            match = MatchNormalized(source="liquipedia", match_id=f"demo-{count}",
                tournament_name=title, tournament_parent=profile.liquipedia_page,
                team1_name="Team Spirit", team2_name="FUT Esports", score1=3, score2=count - 3,
                is_final=True, winner_prize_usd=300000, maps=maps)
            data = media_cards.render_final_card(match, branding=profile.branding)
            save_demo(data, args.output / f"{profile.key}-{count}.png")
        series = Image.new("RGB", (3240, 1080))
        for column, filename in enumerate((f"{profile.key}-announcement.png",
                                             f"{profile.key}-4.png",
                                             f"{profile.key}-standings-1.png")):
            with Image.open(args.output / filename) as image:
                series.paste(image, (column * 1080, 0))
        series.save(args.output / f"{profile.key}-series.png")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
