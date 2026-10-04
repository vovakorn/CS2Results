#!/usr/bin/env python3
"""Render locally; this command has no publishing path."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cs2bot.tournament_preview import PreviewBranding, PreviewTeam, TournamentPreview, format_preview_caption, load_profiles
from cs2bot.tournament_branding import load_theme_registry, resolve_branding
from cs2bot.tournament_preview_cards import render_preview_cards
from cs2bot.match_sources.sources.tournament_preview_source import fetch_tournament_preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--demo", action="store_true")
    selection.add_argument("--profile", help="Profile key")
    parser.add_argument("--draft", action="store_true", help="Render an unapproved local example with a watermark")
    parser.add_argument("--profiles", type=Path, default=ROOT / "data/tournament_preview_profiles.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--teams", type=int, choices=(0, 1, 4, 10, 16, 24, 128), default=16,
                        help="Demo participant count")
    parser.add_argument("--long-names", action="store_true", help="Demo overflow check")
    parser.add_argument("--theme", choices=[theme.key for theme in load_theme_registry().themes],
                        help="Approved visual theme for a marked demo; never changes publication approval")
    args = parser.parse_args()
    if args.draft and args.demo:
        parser.error("--draft needs --profile; --demo already marks fictional data")
    if args.theme and not args.demo:
        parser.error("--theme is only available with --demo")
    if args.demo:
        preview = TournamentPreview.model_validate_json((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
        names = ["Team Spirit", "Vitality", "Natus Vincere", "MOUZ", "G2 Esports", "FaZe Clan",
                 "Team Liquid", "Astralis", "FURIA", "The MongolZ", "Ninjas in Pyjamas", "3DMAX",
                 "Virtus.pro", "BIG", "GamerLegion", "paiN Gaming"]
        teams = [PreviewTeam(team_id=f"demo-{i}", name=(names[i] if i < len(names) else f"Demo Team {i + 1}"))
                 for i in range(args.teams)]
        preview = preview.model_copy(update={"participant_count": args.teams or None, "teams": teams})
        if args.long_names:
            preview = preview.model_copy(update={
                "name": "COUNTER-STRIKE INTERNATIONAL CHAMPIONSHIP — EUROPEAN OPEN SEASON TWO 2026",
                "locations": ["Очень длинное название города, страна · Международная арена имени чемпионов"],
            })
            for team in preview.teams:
                team.name = "International Counter-Strike Championship Academy Division " + team.name
        if args.theme:
            preview = preview.model_copy(update={"branding": resolve_branding(PreviewBranding(theme_key=args.theme))})
    else:
        profiles = [profile for profile in load_profiles(args.profiles) if profile.key == args.profile]
        if len(profiles) != 1:
            parser.error("Unknown profile key")
        preview = asyncio.run(fetch_tournament_preview(profiles[0], allow_draft=args.draft))
    caption = format_preview_caption(preview, draft=args.draft)
    cards = render_preview_cards(preview, demo=args.demo, draft=args.draft)
    args.output.mkdir(parents=True, exist_ok=True)
    for index, card in enumerate(cards, 1):
        (args.output / f"{index:02d}.png").write_bytes(card)
    (args.output / "caption.html").write_text(caption, encoding="utf-8")
    (args.output / "snapshot.json").write_text(preview.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"cards": len(cards), "demo": args.demo, "draft": args.draft, "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
