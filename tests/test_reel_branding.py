"""Exact tournament identities, shared/mixed Reel brands and bounded logo loading."""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from cs2bot import media_cards as m, schedule_reels as r, tournament_visuals as tv
from cs2bot.match_sources.models import SourceReferences, UpcomingMatchNormalized
from cs2bot.tournament_preview import load_profiles

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 9, tzinfo=ZoneInfo("Europe/Moscow"))


def fixtures(count=4):
    profile = load_profiles(ROOT / "data/tournament_preview_profiles.json")[0]
    return [UpcomingMatchNormalized(match_id=f"demo-{index}",
        competition_key="ESL Pro League Season 24", tournament_name="ESL Pro League Season 24",
        source_refs=SourceReferences(serie_id=str(profile.pandascore_serie_id)),
        team1_name="Gaimin Gladiators Academy", team2_name="Natus Vincere Junior",
        scheduled_at=(NOW + timedelta(minutes=30 * index)).isoformat(), best_of=3)
        for index in range(count)]


@pytest.mark.parametrize("kind", ["intro", "matches", "outro"])
def test_single_exact_event_brands_every_scene_without_network(monkeypatch, kind):
    def no_network(*args, **kwargs):
        raise AssertionError("Bundled tournament marks must not require network")
    monkeypatch.setattr(m, "fetch_team_logo", no_network)
    matches = fixtures()
    scene = next(scene for scene in r.storyboard(matches) if scene.kind == kind)
    image = r.render_scene(scene, NOW, len(matches))
    assert image.size == (1080, 1920)
    assert scene.context_matches == tuple(matches)
    for point in ((100, 214), (950, 214)):
        red, green, blue = image.getpixel(point)
        assert green > red * 2 and green > blue * 2


def test_mixed_or_unconfirmed_event_keeps_neutral_shared_header():
    matches = fixtures(5)
    matches[-1] = matches[-1].model_copy(update={"source_refs": None})
    scene = r.storyboard(matches)[1]
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.shared is None
    assert visuals.by_match[matches[0].match_id].branding is not None
    assert visuals.by_match[matches[-1].match_id].branding is None
    image = r.render_scene(scene, NOW, len(matches))
    assert image.getpixel((100, 214)) == r.CYAN
    assert image.getpixel((950, 214)) == r.AMBER
    # Even a homogeneous first page must not claim to represent the whole mixed day.
    top = 550 + (1090 - (4 * 250 + 3 * 24)) // 2
    assert image.getpixel((140, top + 3))[1] > image.getpixel((140, top + 3))[2] * 2


def test_logo_missing_locally_uses_later_provider_url_once_per_animated_scene(monkeypatch):
    matches = fixtures()
    matches[-1] = matches[-1].model_copy(update={"tournament_logo_url": "preview://event"})
    monkeypatch.setattr(tv, "event_logo", lambda *args: None)
    fetched = []
    monkeypatch.setattr(m, "fetch_team_logo", lambda url, **kwargs:
        (fetched.append((url, kwargs["timeout"])), Image.new("RGBA", (100, 100), "white"))[1])
    scene = r.storyboard(matches)[1]
    frames = list(r.animated_scene_frames(scene, NOW, 4, "Europe/Moscow",
        elapsed_before=1.5, total_seconds=8))
    assert len(frames) == 48
    assert fetched == [("preview://event", .75)]


def test_unavailable_mark_preserves_accent_and_does_not_invent_a_logo(monkeypatch):
    matches = fixtures(1)
    matches[0] = matches[0].model_copy(update={"tournament_logo_url": "preview://event"})
    monkeypatch.setattr(tv, "event_logo", lambda *args: None)
    def unavailable(*args, **kwargs):
        raise m.MediaCardError("Unavailable")
    monkeypatch.setattr(m, "fetch_team_logo", unavailable)
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.shared.branding is not None and visuals.shared.logo is None
    assert r.render_scene(r.storyboard(matches)[0], NOW, 1, visuals=visuals).size == (1080, 1920)


def test_expired_logo_budget_skips_provider_download(monkeypatch):
    matches = fixtures(1)
    matches[0] = matches[0].model_copy(update={"tournament_logo_url": "preview://event"})
    monkeypatch.setattr(tv, "event_logo", lambda *args: None)
    monkeypatch.setattr(m, "fetch_team_logo", lambda *args, **kwargs: pytest.fail("Deadline expired"))
    assert r._resolve_reel_visuals(matches, logo_deadline=0).shared.logo is None


def test_shared_and_row_watermarks_leave_original_logo_unchanged(monkeypatch):
    matches = fixtures()
    visuals = r._resolve_reel_visuals(matches)
    original = visuals.shared.logo.tobytes()
    calls = []
    draw = tv.add_event_watermark
    def capture(image, logo, **kwargs):
        calls.append(kwargs)
        draw(image, logo, **kwargs)
    monkeypatch.setattr(tv, "add_event_watermark", capture)
    r.render_scene(r.storyboard(matches)[1], NOW, 4, visuals=visuals)
    assert calls[0]["position"] == (704, 352)
    assert len(calls) == 5
    assert visuals.shared.logo.tobytes() == original


def test_branded_progress_uses_tournament_accent():
    matches = fixtures(1)
    scene = r.ReelScene("matches", tuple(matches), .25, page=1, pages=1)
    frames = list(r.animated_scene_frames(scene, NOW, 1, "Europe/Moscow",
        elapsed_before=0, total_seconds=.25))
    accent = r._resolve_reel_visuals(matches).shared.accent
    assert frames[-1][0].getpixel((978, 1677)) == accent


def test_different_profiles_keep_their_own_colours_and_neutral_global_header(monkeypatch):
    profiles = load_profiles(ROOT / "data/tournament_preview_examples.json")
    matches = fixtures(2)
    monkeypatch.setattr(r, "event_for_match", lambda match, path: profiles[int(match.match_id[-1])])
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.shared is None
    assert [visuals.by_match[match.match_id].accent for match in matches] == [
        tv.accent_color(profile.branding) for profile in profiles]


def test_unknown_events_with_shared_city_label_never_borrow_each_others_logo(monkeypatch):
    matches = [match.model_copy(update={"source_refs": None, "competition_key": "Bucharest",
        "tournament_name": f"Unknown tournament {index}", "tournament_logo_url": f"preview://{index}"})
        for index, match in enumerate(fixtures(2))]
    monkeypatch.setattr(m, "fetch_team_logo", lambda url, **kwargs:
        Image.new("RGBA", (100, 100), "red" if url.endswith("0") else "blue"))
    visuals = r._resolve_reel_visuals(matches)
    assert visuals.shared is None
    first, second = [visuals.by_match[match.match_id] for match in matches]
    assert first.name == "Unknown tournament 0" and second.name == "Unknown tournament 1"
    assert first.logo.getpixel((50, 50))[:3] == (255, 0, 0)
    assert second.logo.getpixel((50, 50))[:3] == (0, 0, 255)
