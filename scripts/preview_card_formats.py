"""Offline square/portrait previews, density checks and phone-size comparisons."""
import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as media
from cs2bot.match_sources.models import MapResult, MatchNormalized, UpcomingMatchNormalized
from cs2bot.tournament_preview import TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat("2026-10-27T12:00:00+03:00")
    logos = ROOT / "video/vrs-major-race/assets/logos"

    def logo(url, **kwargs):
        if url in {"demo://white", "demo://black"}:
            image = Image.new("RGBA", (180, 180))
            ImageDraw.Draw(image).polygon([(90, 10), (170, 170), (10, 170)],
                                         fill="white" if url.endswith("white") else "black")
            return image
        if url and url.startswith("demo://") and (logos / (url[7:] + ".png")).is_file():
            with Image.open(logos / (url[7:] + ".png")) as image:
                return image.convert("RGBA")
        return None

    media.fetch_team_logo = logo
    original_footer = media._card_footer

    def footer(draw, *args, **kwargs):
        original_footer(draw, *args, **kwargs)
        media._draw_text_block(draw, 540, draw._image.height - 30, "МАКЕТ · НЕ ДЛЯ ПУБЛИКАЦИИ", 800,
                              16, display=True, fill=media.AMBER, max_lines=1)

    media._card_footer = footer
    images = {}

    def save(name, data):
        with Image.open(io.BytesIO(data)) as image:
            images[name] = image.convert("RGB")
        images[name].save(args.output / f"{name}.png")

    match = MatchNormalized(source="pandascore", match_id="demo", tournament_name="PGL Masters — Bucharest — Group Stage",
        competition_key="PGL Masters Bucharest 2026", team1_name="Nemiga", team2_name="Dendele CS",
        team1_logo_url="demo://nemiga", team2_logo_url="demo://dendele",
        score1=2, score2=0, best_of=3, date="2026-10-27T09:00:00Z")
    long = match.model_copy(update={"team1_name": "Gaimin Gladiators International",
        "team2_name": "Natus Vincere Junior", "team1_logo_url": "demo://white", "team2_logo_url": "demo://black"})
    for card_format in ("square", "portrait"):
        options = {"card_format": card_format}
        save(f"{card_format}-result", media.render_result_card(match, **options))
        save(f"{card_format}-contrast", media.render_result_card(long, **options))
        save(f"{card_format}-missing", media.render_result_card(long.model_copy(update={
            "team1_logo_url": None, "team2_logo_url": None}), **options))
        for count in (1, 4, 10):
            results = [long.model_copy(update={"match_id": str(i)}) for i in range(count)]
            fixtures = [UpcomingMatchNormalized(match_id=str(i), tournament_name=match.tournament_name,
                competition_key=match.competition_key, team1_name=long.team1_name, team2_name=long.team2_name,
                team1_logo_url=long.team1_logo_url, team2_logo_url=long.team2_logo_url,
                scheduled_at=f"2026-10-27T{10 + i:02}:00:00Z", best_of=3) for i in range(count)]
            for kind, pages in (("digest", media.render_results_cards(results, now, **options)),
                                ("schedule", media.render_schedule_cards(fixtures, now, "Europe/Moscow", **options))):
                for page, data in enumerate(pages, 1):
                    save(f"{card_format}-{kind}-{count}-{page}", data)
        save(f"{card_format}-context", media.render_schedule_context_covers(fixtures[:1], now, **options)[0])
        for count in (3, 4, 5):
            losses = {1} if count < 5 else {1, 3}
            final = long.model_copy(update={"source": "liquipedia", "is_final": True,
                "final_identity_confirmed": True, "winner_prize_usd": 100_000,
                "score1": count - len(losses), "score2": len(losses), "best_of": 3 if count == 3 else 5,
                "maps": [MapResult(name=name, score1=8 if index in losses else 13,
                                   score2=13 if index in losses else 9) for index, name in enumerate(
                         ["Mirage", "Nuke", "Ancient", "Dust II", "Anubis"][:count])]})
            save(f"{card_format}-final-{count}", media.render_final_card(final, **options))
        preview = TournamentPreview.model_validate_json((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
        for page, data in enumerate(render_preview_cards(preview, demo=True, **options), 1):
            save(f"{card_format}-preview-{page}", data)

    def board(items, filename, *, width=440, columns=2):
        gap, label, top = 28, 55, 110
        cell_height = round(width * 1.25) + label
        rows = (len(items) + columns - 1) // columns
        image = Image.new("RGB", (gap + columns * (width + gap), top + rows * cell_height + gap), "#080D19")
        draw = ImageDraw.Draw(image)
        draw.text((gap, 20), "CS2 RESULTS · АДАПТИВНЫЕ КАРТОЧКИ", font=media._font(28, display=True), fill="white")
        draw.text((gap, 65), "Локальные макеты · демонстрационные данные", font=media._font(22, display=True), fill=media.MUTED)
        for index, (key, text) in enumerate(items):
            x, y = gap + index % columns * (width + gap), top + index // columns * cell_height
            draw.text((x, y), text, font=media._font(24, display=True), fill="white")
            source = images[key]
            height = round(source.height * width / source.width)
            image.paste(source.resize((width, height), Image.Resampling.LANCZOS), (x, y + label))
        image.save(args.output / filename)

    comparison = [("square-result", "Telegram / Threads · 1:1"), ("portrait-result", "Instagram · 4:5")]
    board(comparison, "comparison.png")
    board(comparison, "phone-comparison.png", width=320)
    board([(f"{fmt}-{kind}-{count}-1", f"{fmt} · {kind} · {count}")
           for kind in ("schedule", "digest") for count in (1, 4, 10) for fmt in ("square", "portrait")],
          "density.png", width=360, columns=4)
    board([(f"{fmt}-{kind}", f"{fmt} · {kind}") for kind in ("contrast", "missing", "final-5", "preview-1")
           for fmt in ("square", "portrait")], "stress.png", width=440)
    manifest = {"renders": len(images), "formats": {key: list(image.size) for key, image in images.items()},
                "network": "disabled", "published": False}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"renders": len(images), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
