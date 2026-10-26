"""Render an offline comparison and density checks; every result is fictional."""
from datetime import datetime
from io import BytesIO
from pathlib import Path
import argparse
import json
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw
from cs2bot import media_cards as media
from cs2bot.match_sources.models import MapResult, MatchNormalized
from cs2bot.tournament_branding import branding_for_tournament
from cs2bot.tournament_preview import TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cover

EVENTS = [("pgl", "PGL Masters Bucharest 2026", "PGL · КРАСНЫЙ АКЦЕНТ"),
          ("iem", "IEM Beijing 2026", "IEM · ЖЁЛТЫЙ АКЦЕНТ"),
          ("blast", "BLAST Rivals Hong Kong 2026", "BLAST · БИРЮЗОВЫЙ АКЦЕНТ"),
          ("major", "PGL Major Singapore 2026", "MAJOR · МЕДНЫЙ АКЦЕНТ")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--logos", type=Path, help="Optional local vitality.png and natus-vincere.png")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    images = {}

    def logo(url, **kwargs):
        name = {"demo://vitality": "vitality.png", "demo://navi": "natus-vincere.png"}.get(url)
        if name and args.logos and (args.logos / name).is_file():
            with Image.open(args.logos / name) as image:
                return image.convert("RGBA")
        if url in ("demo://white", "demo://black"):
            image = Image.new("RGBA", (180, 180))
            ImageDraw.Draw(image).polygon([(90, 10), (170, 170), (10, 170)],
                                         fill="white" if url.endswith("white") else "black")
            return image
        return None

    media.fetch_team_logo = logo
    original_footer = media._card_footer

    def footer(draw, *values, **kwargs):
        original_footer(draw, *values, **kwargs)
        draw.rectangle((38, 993, 1040, 1045), fill=media.NAVY)
        media._draw_text_block(draw, 540, 1018, "МАКЕТ · ВЫМЫШЛЕННЫЕ ДАННЫЕ", 940, 22,
                              display=True, fill=media.AMBER, max_lines=1)

    media._card_footer = footer

    def save(key, data):
        with Image.open(BytesIO(data)) as image:
            assert image.size == (1080, 1080), (key, image.size)
            images[key] = image.convert("RGB")
        images[key].save(args.output / f"{key}.png")

    for key, title, _ in EVENTS:
        match = MatchNormalized(source="pandascore", match_id=f"demo-{key}", tournament_name=title,
            competition_key=title, team1_name="Vitality", team2_name="NAVI",
            team1_logo_url="demo://vitality", team2_logo_url="demo://navi", score1=2, score2=1, best_of=3,
            maps=[MapResult(name=name, score1=a, score2=b) for name, a, b in
                  [("Mirage", 13, 9), ("Nuke", 8, 13), ("Dust II", 13, 10)]])
        save(f"{key}-result", media.render_result_card(match))
        preview = TournamentPreview.model_validate_json((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
        preview = preview.model_copy(update={"name": title, "branding": branding_for_tournament(title)})
        save(f"{key}-announcement", render_preview_cover(preview, demo=True))
        for count in (1, 4, 10):
            matches = [match.model_copy(update={"match_id": f"demo-{key}-{i}"}) for i in range(count)]
            for page, data in enumerate(media.render_results_cards(matches, now), 1):
                save(f"{key}-digest-{count}-{page}", data)
        stress = match.model_copy(update={
            "tournament_name": title + " · International Championship Playoffs",
            "team1_name": "Gaimin Gladiators Academy", "team2_name": "Natus Vincere Junior",
            "team1_logo_url": "demo://white", "team2_logo_url": "demo://black"})
        save(f"{key}-contrast", media.render_result_card(stress))
        save(f"{key}-missing", media.render_result_card(stress.model_copy(update={
            "team1_logo_url": None, "team2_logo_url": None})))
        final = match.model_copy(update={"source": "liquipedia", "is_final": True,
            "final_identity_confirmed": True, "winner_prize_usd": 100000})
        save(f"{key}-final", media.render_final_card(final))

    def board(items, filename, columns=2, size=1080):
        rows = (len(items) + columns - 1) // columns
        margin, gap, label = 40, 40, 85
        image = Image.new("RGB", (margin * 2 + columns * size + (columns - 1) * gap,
                                   170 + rows * (size + label)), "#080D19")
        draw = ImageDraw.Draw(image)
        draw.text((40, 25), "CS2 RESULTS · ПОДЛОЖКА СЧЁТА", font=media._font(40, display=True), fill="white")
        draw.text((40, 85), "Мягкий градиент и тонкий контур · вымышленные результаты",
                  font=media._font(25, display=True), fill=media.MUTED)
        for i, (key, caption) in enumerate(items):
            x, y = margin + i % columns * (size + gap), 170 + i // columns * (size + label)
            image.paste(images[key].resize((size, size), Image.Resampling.LANCZOS), (x, y))
            draw.text((x+10, y+size+15), caption, font=media._font(25, display=True), fill="white")
        image.save(args.output / filename, optimize=True)

    board([(f"{key}-result", label) for key, _, label in EVENTS], "result-themes.png")
    board([(f"{key}-{kind}", label + (" · АНОНС" if kind == "announcement" else " · РЕЗУЛЬТАТ"))
           for key, _, label in EVENTS for kind in ("announcement", "result")],
          "announcement-result-comparison.png", size=780)
    board([(f"{key}-digest-{count}-1", f"{key.upper()} · {count} {'матч' if count == 1 else 'матча' if count == 4 else 'матчей'} · первая страница")
           for key, _, _ in EVENTS for count in (1, 4, 10)], "density.png", columns=3, size=500)
    board([(f"{key}-{kind}", key.upper() + " · " + kind) for key, _, _ in EVENTS
           for kind in ("contrast", "missing")], "logos-and-long-names.png", size=780)
    print(json.dumps({"renders": len(images), "dimensions": [1080, 1080], "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
