"""Layout regressions: both formats must keep the same facts readable."""
import io
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from cs2bot import media_cards as media
from cs2bot.match_sources.models import (
    MapResult, MatchNormalized, RadarBracketNode, TournamentPlacement,
    TournamentRadar, TournamentVRSImpact, UpcomingMatchNormalized,
)
from cs2bot.tournament_preview import PreviewTeam, TournamentPreview
from cs2bot.tournament_preview_cards import render_preview_cards

NOW = datetime.fromisoformat("2026-10-27T12:00:00+03:00")


@pytest.fixture(autouse=True)
def offline_logos(monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)


def result(**updates):
    return MatchNormalized(source="pandascore", match_id="demo", tournament_name="PGL Masters Bucharest 2026",
        team1_name="Gaimin Gladiators International", team2_name="Natus Vincere Junior",
        score1=2, score2=1, best_of=3, date="2026-10-27T09:00:00Z").model_copy(update=updates)


def cards(kind, card_format):
    options = {"card_format": card_format}
    match = result()
    maps = [MapResult(name=name, score1=a, score2=b) for name, a, b in
            [("Mirage", 13, 9), ("Nuke", 8, 13), ("Ancient", 13, 10)]]
    upcoming = [UpcomingMatchNormalized(match_id=str(i), tournament_name=match.tournament_name,
        team1_name=match.team1_name, team2_name=match.team2_name,
        scheduled_at=f"2026-10-27T{10 + i:02}:00:00Z", best_of=3) for i in range(10)]
    if kind == "result":
        return [media.render_result_card(match, **options)]
    if kind == "maps":
        return [media.render_result_card(match.model_copy(update={"maps": maps}), **options)]
    if kind == "final":
        return [media.render_final_card(match.model_copy(update={"maps": maps, "source": "liquipedia",
            "is_final": True, "final_identity_confirmed": True, "winner_prize_usd": 100_000}), **options)]
    if kind == "schedule":
        return media.render_schedule_cards(upcoming, NOW, "Europe/Moscow", **options)
    if kind == "digest":
        return media.render_results_cards([match.model_copy(update={"match_id": str(i)}) for i in range(10)], NOW, **options)
    if kind == "context":
        return media.render_schedule_context_covers(upcoming[:1], NOW, **options)
    if kind == "radar":
        radar = TournamentRadar(tournament_id="demo",
            bracket_structure=[RadarBracketNode(match_id=str(i), round_name="Quarter-final",
                team1_name=match.team1_name, team2_name=match.team2_name) for i in range(6)])
        return media.render_tournament_radar_cards(radar, match.tournament_name, "Europe/Moscow", **options)
    if kind == "standings":
        placements = [TournamentPlacement(placement=str(i + 1), team_name=match.team1_name,
            prize_usd=100_000 // (i + 1)) for i in range(8)]
        return media.render_tournament_standings_cards(match.tournament_name, placements, **options)
    if kind == "vrs":
        impacts = [TournamentVRSImpact(placement=str(i + 1), team_name=match.team1_name, team_id=str(i),
            before_points=1000, after_points=1030, before_rank=i + 20, after_rank=i + 10,
            points_delta=30, rank_delta=10, source="Valve VRS", before_version="demo-before",
            after_version="demo-after", before_effective_at="2026-10-01", after_effective_at="2026-10-15")
            for i in range(8)]
        return media.render_tournament_vrs_cards(match.tournament_name, impacts, **options)
    preview = TournamentPreview.model_validate_json(
        (Path(__file__).parent / "fixtures/tournament_preview_demo.json").read_text())
    preview = preview.model_copy(update={"participant_count": 16,
        "teams": [PreviewTeam(team_id=str(i), name="Team") for i in range(16)]})
    return render_preview_cards(preview, demo=True, **options)


@pytest.mark.parametrize("card_format,expected", [("square", (1080, 1080)), ("portrait", (1080, 1350))])
@pytest.mark.parametrize("kind", ["result", "maps", "final", "schedule", "digest", "context", "radar", "standings", "vrs", "preview"])
def test_every_static_layout_fits_and_keeps_text_inside_card(monkeypatch, card_format, expected, kind):
    bounds = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *args, **kwargs):
        if draw._image.size == expected and draw._image.mode in {"RGB", "RGBA"}:
            bounds.append((text, draw.textbbox(xy, text, font=kwargs.get("font"))))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    rendered = cards(kind, card_format)
    assert rendered and bounds
    for data in rendered:
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == expected and image.mode == "RGB"
    for text, (left, top, right, bottom) in bounds:
        assert 32 <= left <= right <= expected[0] - 32, (kind, text, (left, right))
        assert 32 <= top <= bottom <= expected[1] - 20, (kind, text, (top, bottom))


@pytest.mark.parametrize("card_format", ["square", "portrait"])
def test_four_long_fixture_names_do_not_touch_score_cells(monkeypatch, card_format):
    bounds = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *args, **kwargs):
        if "GLADIATORS" in text or "JUNIOR" in text:
            bounds.append(draw.textbbox(xy, text, font=kwargs.get("font")))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    media.render_results_card([result(match_id=str(i)) for i in range(4)], NOW, card_format=card_format)
    assert bounds
    assert all(right < 457 or left > 623 for left, _, right, _ in bounds)


def test_unknown_card_format_is_rejected():
    with pytest.raises(media.MediaCardError, match="Unsupported static card format"):
        media.render_result_card(result(), card_format="story")
