"""Offline visual QA for the complete tournament catalog; all match data is fictional."""
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import argparse
import json
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import (MapResult, MatchNormalized, TournamentPlacement,
    TournamentVRSImpact, UpcomingMatchNormalized)
from cs2bot.tournament_branding import load_theme_registry
from cs2bot.tournament_visuals import branding_for_match
from preview_schedule_reels import _synthetic_logo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    m.fetch_team_logo = _synthetic_logo
    now = datetime(2026, 10, 16, 9, tzinfo=ZoneInfo("Europe/Moscow"))
    names = [("Gaimin Gladiators Academy", "Natus Vincere Junior"), ("NAVI", "FURIA"),
             ("Team Spirit", "The MongolZ"), ("International Championship Academy", "FUT Esports")]
    images = {}

    def fixtures(name, count):
        return [UpcomingMatchNormalized(match_id=f"demo-{i}", tournament_name=name, competition_key=name,
            team1_name=names[i % 4][0], team2_name=names[i % 4][1],
            team1_logo_url="preview://light" if i % 3 == 0 else None,
            team2_logo_url="preview://dark" if i % 3 == 0 else None,
            scheduled_at=(now + timedelta(hours=1, minutes=i * 30)).isoformat(), best_of=3)
            for i in range(count)]

    def save(name, data):
        image = Image.open(BytesIO(data)).convert("RGB") if isinstance(data, bytes) else data.convert("RGB")
        if image.height == 1080:
            draw = ImageDraw.Draw(image)
            draw.rectangle((38, 993, 1040, 1045), fill=m.NAVY)
            m._draw_text_block(draw, 540, 1018, "МАКЕТ · ВЫМЫШЛЕННЫЕ ДАННЫЕ", 940, 22,
                               display=True, fill=m.AMBER, max_lines=1)
        image.save(args.output / f"{name}.png")
        images[name] = image

    for rule in load_theme_registry().match_rules:
        key = f"catalog-{rule.theme_key}"
        if key not in images:
            save(key, m.render_schedule_cards(fixtures(rule.names[0], 4), now, "Europe/Moscow")[0])

    for count in (1, 4, 10):
        matches = fixtures("Thunderpick World Championship 2026", count)
        results = [MatchNormalized(source="pandascore", match_id=match.match_id,
            tournament_name=match.tournament_name, competition_key=match.competition_key,
            team1_name=match.team1_name, team2_name=match.team2_name,
            team1_logo_url=match.team1_logo_url, team2_logo_url=match.team2_logo_url,
            score1=2, score2=1, best_of=3,
            maps=[MapResult(name=name, score1=a, score2=b)
                  for name, a, b in (("Mirage", 13, 9), ("Nuke", 8, 13), ("Dust II", 16, 14))])
            for match in matches]
        for kind, pages in (("schedule", m.render_schedule_cards(matches, now, "Europe/Moscow")),
                            ("digest", m.render_results_cards(results, now))):
            for index, data in enumerate(pages, 1):
                save(f"thunderpick-{kind}-{count}-{index}", data)
        if count == 4:
            save("thunderpick-context", m.render_schedule_context_covers(matches, now)[0])
            save("thunderpick-result", m.render_result_card(results[0]))
            save("thunderpick-result-no-logos", m.render_result_card(results[1]))
            save("thunderpick-long-title", m.render_schedule_cards([
                match.model_copy(update={"tournament_name": match.tournament_name + " · International Championship Finals",
                                         "competition_key": match.competition_key + " · International Championship Finals"})
                for match in matches], now, "Europe/Moscow")[0])
            for index, scene in enumerate(r.storyboard(matches)):
                save(f"thunderpick-reel-{index}", r.render_scene(scene, now, count, preview_watermark=True))

    brand = branding_for_match(fixtures("Thunderpick World Championship 2026", 1)[0])
    final = results[0].model_copy(update={"source": "liquipedia", "is_final": True,
        "final_identity_confirmed": True, "winner_prize_usd": 100000})
    save("thunderpick-final", m.render_final_card(final))
    placements = [TournamentPlacement(placement=str(i + 1), team_name=names[i % 4][0],
                                     prize_usd=100000 // (i + 1)) for i in range(10)]
    impacts = [TournamentVRSImpact(placement=str(i + 1), team_name=item.team_name, team_id=str(i),
        before_points=1000, after_points=1030, before_rank=i + 20, after_rank=i + 10,
        points_delta=30, rank_delta=10, source="Valve VRS", before_version="demo-before",
        after_version="demo-after", before_effective_at="2026-10-01", after_effective_at="2026-10-15")
        for i, item in enumerate(placements)]
    for kind, pages in (("standings", m.render_tournament_standings_cards("Thunderpick World Championship 2026",
                        placements, branding=brand)),
                        ("vrs", m.render_tournament_vrs_cards("Thunderpick World Championship 2026", impacts, branding=brand))):
        for index, data in enumerate(pages, 1):
            save(f"thunderpick-{kind}-{index}", data)

    def board(keys, filename, columns=3):
        rows = (len(keys) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * 440, rows * 475 + 60), m.NAVY)
        draw = ImageDraw.Draw(sheet)
        draw.text((18, 18), "МАКЕТЫ · ВЫМЫШЛЕННЫЕ ДАННЫЕ", font=m._font(23, display=True), fill=m.WHITE)
        for i, key in enumerate(keys):
            x, y = i % columns * 440 + 10, i // columns * 475 + 60
            sheet.paste(images[key].resize((420, 420), Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 8, y + 428), key.replace("thunderpick-", ""), font=m._font(22), fill=m.MUTED)
        sheet.save(args.output / filename)

    board([key for key in images if key.startswith("catalog-")], "catalog.png", 4)
    board(["thunderpick-schedule-4-1", "thunderpick-context", "thunderpick-result",
           "thunderpick-digest-4-1", "thunderpick-final", "thunderpick-vrs-1"], "thunderpick-series.png")
    board([f"thunderpick-{kind}-{count}-1" for kind in ("schedule", "digest") for count in (1, 4, 10)],
          "density.png")
    print(json.dumps({"pngs": len(images), "catalog_themes": sum(k.startswith("catalog-") for k in images),
                      "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
