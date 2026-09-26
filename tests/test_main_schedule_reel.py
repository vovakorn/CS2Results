import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from cs2bot import main
from cs2bot.match_sources.models import UpcomingMatchNormalized
from cs2bot.reel_delivery import PendingReel


NOW = datetime(2026, 9, 25, 9, 15, tzinfo=ZoneInfo("Europe/Moscow"))


def match(index):
    return UpcomingMatchNormalized(
        match_id=f"m-{index}", tournament_name="IEM Test",
        team1_name="Spirit", team2_name="Vitality",
        scheduled_at=f"2026-09-25T{10 + index // 2:02d}:{30 * (index % 2):02d}:00+03:00",
        feature_reason="tier1_tournament",
    )


def test_reel_disabled_does_not_fetch(monkeypatch):
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: False)
    async def forbidden(*args):
        raise AssertionError("fetched while disabled")
    monkeypatch.setattr(main, "fetch_upcoming_matches", forbidden)

    response = main._handle_schedule_reel_job(False, None)

    assert json.loads(response["body"])["skipped_reason"] == "disabled"


def test_runtime_probe_is_offline_and_never_publishes(monkeypatch):
    monkeypatch.setattr(main, "probe_reel_runtime", lambda now: {"render_ms": 10, "mp4_bytes": 100})
    async def forbidden(*args):
        raise AssertionError("source fetched during runtime probe")
    monkeypatch.setattr(main, "fetch_upcoming_matches", forbidden)
    monkeypatch.setattr(main, "upload_public_reel", lambda *args: (_ for _ in ()).throw(AssertionError("uploaded")))

    response = main._handle_schedule_reel_job(True, None, True)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["render_probe"]["mp4_bytes"] == 100


def test_reel_dry_run_uses_all_selected_matches_without_publishing(monkeypatch):
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: False)
    monkeypatch.setattr(main, "_local_day_window", lambda: (NOW, NOW, NOW))
    async def fetched(*args):
        return [match(2), match(0), match(1)]
    monkeypatch.setattr(main, "fetch_upcoming_matches", fetched)
    monkeypatch.setattr(main, "_select_schedule_matches", lambda matches: matches)
    monkeypatch.setattr(main, "render_schedule_reel", lambda *args: (_ for _ in ()).throw(AssertionError("encoded on dry run")))

    response = main._handle_schedule_reel_job(True, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["match_ids"] == ["m-0", "m-1", "m-2"]
    assert body["duration_seconds"] == 8


def test_reel_over_limit_does_not_publish_partial_day(monkeypatch):
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: True)
    monkeypatch.setattr(main, "_local_day_window", lambda: (NOW, NOW, NOW))
    async def fetched(*args):
        return [match(index) for index in range(21)]
    monkeypatch.setattr(main, "fetch_upcoming_matches", fetched)
    monkeypatch.setattr(main, "_select_schedule_matches", lambda matches: matches)

    response = main._handle_schedule_reel_job(True, None)
    body = json.loads(response["body"])

    assert body["skipped_reason"] == "too_many_matches"
    assert body["matches_selected"] == 21


def test_existing_reel_state_is_not_rerendered(monkeypatch):
    state = PendingReel("2026-09-25", "12345", 4, NOW.isoformat())
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: True)
    monkeypatch.setattr(main, "_local_day_window", lambda: (NOW, NOW, NOW))
    async def fetched(*args):
        return [match(index) for index in range(4)]
    monkeypatch.setattr(main, "fetch_upcoming_matches", fetched)
    monkeypatch.setattr(main, "_select_schedule_matches", lambda matches: matches)
    monkeypatch.setattr(main, "load_pending_reel", lambda day: state)
    monkeypatch.setattr(main, "render_schedule_reel", lambda *args: (_ for _ in ()).throw(AssertionError("rerendered")))
    monkeypatch.setattr(main, "advance_pending_reel", lambda *args: "processing")

    response = main._handle_schedule_reel_job(False, None)

    assert json.loads(response["body"])["state"] == "processing"
