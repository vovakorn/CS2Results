"""Visual aliases must not depend on, or expand, editorial publication profiles."""
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
import pytest
from pydantic import ValidationError

from cs2bot import media_cards as media, schedule_reels as reels
from cs2bot.match_sources.models import (MapResult, MatchNormalized, SourceReferences,
    TournamentPlacement, TournamentVRSImpact, UpcomingMatchNormalized)
from cs2bot.match_sources.sources.pandascore_source import _normalize_upcoming_item
from cs2bot.tournament_branding import ThemeRegistry, branding_for_tournament, load_theme_registry
from cs2bot.tournament_identity import event_for_match
from cs2bot.tournament_preview import PreviewBranding, PreviewProfile, TournamentPreview
from cs2bot.tournament_visuals import branding_for_match, branding_for_matches

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 16, 9, tzinfo=ZoneInfo("Europe/Moscow"))
NAMES = [
    ("ESL Pro League Season 25 2027", "epl"),
    ("IEM Beijing 2026", "iem-beijing"),
    ("Intel Extreme Masters Cologne 2027", "iem-base"),
    ("BLAST Premier Rivals Hong Kong 2026", "blast-rivals"),
    ("BLAST Open Singapore 2027", "blast-open"),
    ("BLAST Bounty Season 1 2027", "blast-bounty"),
    ("PGL Masters Bucharest 2026", "pgl"),
    ("PGL Singapore Major 2026", "pgl-major"),
    ("Thunderpick World Championship 2026", "thunderpick"),
    ("FISSURE Playground 3 2027", "fissure"),
    ("StarLadder StarSeries Season 21 2027", "starseries"),
    ("FiReMAJOR Buenos Aires 2027", "fire"),
    ("CS Asia Championships 2027", "cac"),
    ("Perfect World Shanghai Major 2027", "pw-major"),
    ("Esports World Cup 2027", "ewc"),
    ("Esports Nations Cup 2027", "enc"),
    ("ESL Event 7 2028", "neutral"),
    ("BLAST Event 2028", "blast-base"),
]


def fixture(name="Thunderpick World Championship 2026", index=0):
    return UpcomingMatchNormalized(match_id=str(index), tournament_name=name,
        competition_key=name, scheduled_at=f"2026-10-16T{10 + index:02d}:00:00+03:00",
        team1_name="Gaimin Gladiators Academy", team2_name="Natus Vincere Junior", best_of=3)


@pytest.mark.parametrize("name,key", NAMES)
def test_every_catalog_theme_is_available_without_an_editorial_profile(name, key):
    match = fixture(name)
    assert event_for_match(match, ROOT / "data/tournament_preview_profiles.json") is None
    before = match.model_dump_json()
    brand = branding_for_match(match)
    assert brand.theme_key == key
    assert brand.background and brand.motif and brand.theme_source_urls
    assert match.model_dump_json() == before


def test_catalog_examples_cover_all_themes():
    assert {key for _, key in NAMES} == {theme.key for theme in load_theme_registry().themes}


@pytest.mark.parametrize("name,key", [("Thunderpick Event", "thunderpick"),
    ("FISSURE Universe", "fissure"), ("StarLadder Event", "starseries"),
    ("Esports Foundation Event", "neutral")])
def test_organizer_fallback_works_without_a_known_series(name, key):
    assert branding_for_tournament(name).theme_key == key


def test_new_profile_and_preview_infer_visuals_without_enabling_editorial_approval():
    data = load_theme_registry().model_dump(mode="json")
    profile_data = json.loads((ROOT / "data/tournament_preview_profiles.json").read_text())[0]
    profile_data.update(key="thunderpick-world-championship-2026", approved=False,
        liquipedia_page="Thunderpick/World_Championship/2026", branding={})
    profile = PreviewProfile.model_validate(profile_data)
    assert profile.branding.theme_key == "thunderpick" and not profile.approved
    preview_data = json.loads((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
    preview_data.update(name="Thunderpick World Championship 2026", branding={})
    preview = TournamentPreview.model_validate(preview_data)
    assert preview.branding.theme_key == "thunderpick"
    # An explicit reviewed color survives automatic theme selection and replay.
    profile_data["branding"] = {"accent": "#123456"}
    custom = PreviewProfile.model_validate(profile_data)
    assert custom.branding.accent == "#123456"
    assert PreviewProfile.model_validate_json(custom.model_dump_json()).branding == custom.branding
    assert load_theme_registry().model_dump(mode="json") == data


def test_legacy_neutral_snapshot_keeps_its_saved_visuals_on_retry():
    data = json.loads((ROOT / "tests/fixtures/tournament_preview_demo.json").read_text())
    data.update(name="Thunderpick World Championship 2026",
                branding=PreviewBranding(accent="#16C7FF").model_dump(mode="json"))
    preview = TournamentPreview.model_validate(data)
    assert preview.branding.theme_key is None and preview.branding.background is None
    assert TournamentPreview.model_validate_json(preview.model_dump_json()).branding == preview.branding


@pytest.mark.parametrize("league,series,key", [
    ("Thunderpick", "Thunderpick World Championship 2026", "thunderpick"),
    ("IEM", "IEM Beijing 2026", "iem-beijing"),
    ("BLAST Premier", "BLAST Premier Rivals Hong Kong 2026", "blast-rivals"),
])
def test_real_pandascore_name_composition_keeps_event_over_series_priority(league, series, key):
    match = _normalize_upcoming_item({"id": 42, "status": "not_started",
        "league": {"name": league}, "serie": {"name": series},
        "tournament": {"name": "Group Stage"}, "scheduled_at": NOW.isoformat(),
        "opponents": [{"opponent": {"id": 1, "name": "Legacy"}},
                      {"opponent": {"id": 2, "name": "FURIA"}}]})
    assert match is not None
    assert branding_for_match(match).theme_key == key


@pytest.mark.parametrize("page,key", [
    ("Thunderpick/World_Championship/2026", "thunderpick"),
    ("Intel_Extreme_Masters/2026/Beijing", "iem-beijing"),
    ("PGL/2026/Singapore_Major", "pgl-major"),
    ("BLAST/Premier/Open/2027", "blast-open"),
    ("CS_Asia_Championships/2027", "cac"),
])
def test_liquipedia_parent_aliases_share_the_visual_theme(page, key):
    match = MatchNormalized(source="liquipedia", tournament_name="Playoffs", tournament_parent=page,
        team1_name="Legacy", team2_name="FURIA", score1=2, score2=1)
    assert branding_for_match(match).theme_key == key


@pytest.mark.parametrize("name", ["PGL Major 2028", "IEM Cologne Major 2027",
    "BLAST Paris Major 2028", "Perfect World Shanghai Major 2028"])
def test_unknown_major_never_borrows_ordinary_or_another_major_logo(name):
    brand = branding_for_tournament(name)
    assert brand.theme_key == "neutral" and brand.logo_asset is None


@pytest.mark.parametrize("name", ["NotThunderpick World Championship", "PGLish Cup", "IEMish Cup",
    "Community Cup featuring BLAST", "CCT Europe 2026", "CACademy Cup"])
def test_unregistered_name_and_substring_matches_keep_channel_fallback(name):
    assert branding_for_tournament(name) is None


def test_missing_profile_file_does_not_disable_the_visual_catalog(tmp_path):
    assert branding_for_matches([fixture()], tmp_path / "missing.json").theme_key == "thunderpick"


def test_conflicting_aliases_never_choose_an_arbitrary_logo():
    assert branding_for_tournament("PGL", competition_key="BLAST") is None


def test_shared_style_does_not_merge_different_tournaments_or_sources():
    first = fixture().model_copy(update={"source_refs": SourceReferences(serie_id="100")})
    second = fixture(index=1).model_copy(update={"source_refs": SourceReferences(serie_id="101")})
    assert branding_for_matches([first, second]) is None
    assert branding_for_matches([fixture(), fixture("Thunderpick World Championship 2027")]) is None


@pytest.mark.parametrize("fault", ["empty", "duplicate", "unknown"])
def test_visual_alias_registry_rejects_invalid_references(fault):
    data = load_theme_registry().model_dump(mode="json")
    if fault == "empty":
        data["match_rules"][0]["names"] = ["---"]
    elif fault == "duplicate":
        data["match_rules"][1]["names"].append(data["match_rules"][0]["names"][0])
    else:
        data["match_rules"][0]["theme_key"] = "missing"
    with pytest.raises(ValidationError):
        ThemeRegistry.model_validate(data)


@pytest.mark.parametrize("kind", ["schedule", "context", "result", "digest", "final", "standings", "vrs", "reel"])
def test_thunderpick_uses_palette_and_motif_in_every_active_fixture_format(kind, monkeypatch):
    monkeypatch.setattr(media, "fetch_team_logo", lambda *args, **kwargs: None)
    match = fixture()
    brand = branding_for_match(match)
    if kind == "schedule":
        image = Image.open(BytesIO(media.render_schedule_cards([match], NOW, "Europe/Moscow")[0]))
    elif kind == "context":
        image = Image.open(BytesIO(media.render_schedule_context_covers([match], NOW)[0]))
    elif kind == "reel":
        visuals = reels._resolve_reel_visuals([match])
        assert visuals.shared.branding.theme_key == "thunderpick"
        image = reels.render_scene(reels.storyboard([match])[0], NOW, 1, visuals=visuals)
    elif kind == "final":
        result = MatchNormalized(source="liquipedia", tournament_name=match.tournament_name,
            team1_name=match.team1_name, team2_name=match.team2_name, score1=3, score2=0,
            is_final=True, final_identity_confirmed=True, winner_prize_usd=100000,
            maps=[MapResult(name=name, score1=13, score2=8) for name in ("Nuke", "Mirage", "Dust II")])
        image = Image.open(BytesIO(media.render_final_card(result)))
    elif kind == "standings":
        placements = [TournamentPlacement(placement=str(i + 1), team_name=f"Team {i}", prize_usd=1000)
                      for i in range(4)]
        image = Image.open(BytesIO(media.render_tournament_standings_cards(match.tournament_name,
            placements, branding=brand)[0]))
    elif kind == "vrs":
        impacts = [TournamentVRSImpact(placement=str(i + 1), team_name=f"Team {i}", team_id=str(i),
            before_points=1000, after_points=1020, before_rank=i + 20, after_rank=i + 10,
            points_delta=20, rank_delta=10, source="Valve VRS", before_version="before",
            after_version="after") for i in range(4)]
        image = Image.open(BytesIO(media.render_tournament_vrs_cards(match.tournament_name,
            impacts, branding=brand)[0]))
    else:
        result = MatchNormalized(source="pandascore", tournament_name=match.tournament_name,
            team1_name=match.team1_name, team2_name=match.team2_name, score1=2, score2=1)
        data = (media.render_result_card(result) if kind == "result" else
                media.render_results_cards([result], NOW)[0])
        image = Image.open(BytesIO(data))
    assert image.size == ((1080, 1920) if kind == "reel" else (1080, 1350))
    assert image.getpixel((10, 10))[:3] == (9, 23, 33)
    # Background alone must retain the catalog's pulse, not merely its accent.
    themed = media._fixture_canvas(brand, (1080, 1080))
    flat = media._fixture_canvas(brand.model_copy(update={"motif": None}), (1080, 1080))
    assert themed.tobytes() != flat.tobytes()


def test_mixed_reel_keeps_shared_header_neutral_and_each_tournament_style():
    matches = [fixture(), fixture("FISSURE Playground 3 2027", 1)]
    visuals = reels._resolve_reel_visuals(matches)
    assert visuals.shared is None
    assert [visuals.by_match[match.match_id].branding.theme_key for match in matches] == ["thunderpick", "fissure"]
