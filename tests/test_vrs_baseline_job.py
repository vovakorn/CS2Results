"""Real immutable storage probes for pre-event VRS, with all network mocked."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from cs2bot import main, tournament_preview_job, vrs_baseline_job as job
from cs2bot.match_sources import storage
from cs2bot.match_sources.models import MatchNormalized, TournamentPlacement, VRSRankingSnapshot, VRSTeamSnapshot
from cs2bot.match_sources.sources.tournament_preview_source import PreviewCalendarScan
from cs2bot.match_sources.sources import tournament_preview_source as calendar_source
from cs2bot.match_sources.sources.vrs_source import VRSUnavailableError
from test_storage import FakeS3

NOW = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
PAGE = "PGL/2026/Test"


def snapshot(version="before", effective="2026-10-05T00:00:00Z", fetched=None):
    return VRSRankingSnapshot(source="Valve VRS live/global", version=version,
        effective_at=effective, fetched_at=fetched or NOW.isoformat(),
        teams=[VRSTeamSnapshot(team_id="nemiga", team_name="Nemiga", points=1000, rank=20),
               VRSTeamSnapshot(team_id="furia", team_name="FURIA", points=1500, rank=5)])


def event(page=PAGE, start="2026-11-02"):
    return {"pagename": page, "name": "Future PGL", "start": start, "end": "2026-11-08",
            "source_url": "https://liquipedia.net/counterstrike/" + page}


@pytest.fixture
def runtime(monkeypatch):
    s3, alerts, fetches = FakeS3(), [], []
    original_list = s3.list_objects_v2
    # boto3 supplies a default MaxKeys; the shared outbox fake requires it explicitly.
    s3.list_objects_v2 = lambda **kwargs: original_list(**{"MaxKeys": 1000, **kwargs})
    events, issues = [event()], []
    async def calendar(now, *, require_prize_pool):
        assert require_prize_pool is False
        return PreviewCalendarScan(events.copy(), issues.copy())
    async def fetch():
        fetches.append(True)
        return snapshot()
    def forbidden(*args, **kwargs):
        pytest.fail("baseline probes must never access external services")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(job, "fetch_preview_calendar", calendar)
    monkeypatch.setattr(job, "fetch_snapshot", fetch)
    monkeypatch.setattr(storage, "_client", lambda: s3)
    monkeypatch.setattr(storage, "_bucket", lambda: "test-bucket")
    r = SimpleNamespace(ENABLE_VRS=True, ENABLE_TOURNAMENT_PREVIEWS=False,
                        _notify_admin=lambda *args: alerts.append(args))
    return SimpleNamespace(r=r, s3=s3, alerts=alerts, fetches=fetches, events=events, issues=issues)


def run(runtime, *, dry_run=False, now=NOW):
    result = job.run_vrs_baseline_job(dry_run, runtime.r, now=now)
    return result["statusCode"], json.loads(result["body"])


def read_before(page=PAGE):
    return asyncio.run(storage.read_vrs_snapshot(job.calendar_baseline_id(page), "before", "initial"))


def test_capture_without_profile_preview_or_publication_and_reuse_immutable_initial(runtime):
    status, body = run(runtime)
    assert status == 200 and body["events"][0]["status"] == "saved"
    assert read_before() == snapshot()
    assert not any(key.startswith(("processed/", "claims/", "outbox/")) for key in runtime.s3.objects)
    first = read_before().model_dump()
    assert run(runtime, now=NOW + timedelta(hours=1))[1]["events"][0]["status"] == "existing"
    assert read_before().model_dump() == first
    assert len(runtime.fetches) == 1 and not runtime.alerts


def test_dry_run_reads_real_readiness_but_never_writes_or_alerts(runtime):
    status, body = run(runtime, dry_run=True)
    assert status == 200 and body["events"][0]["status"] == "would_capture"
    assert runtime.s3.objects == {} and runtime.alerts == []


def test_one_source_fetch_covers_multiple_events_and_exact_pages_never_collide(runtime):
    runtime.events.append(event("PGL_2026_Test"))
    assert run(runtime)[0] == 200
    assert len(runtime.fetches) == 1
    assert job.calendar_baseline_id(PAGE) != job.calendar_baseline_id("PGL_2026_Test")
    assert read_before() == read_before("PGL_2026_Test") == snapshot()


def test_source_failure_is_recorded_and_next_invocation_retries_before_start(runtime, monkeypatch):
    calls = []
    async def fetch():
        calls.append(True)
        if len(calls) == 1:
            raise VRSUnavailableError("private transport details must not appear")
        return snapshot()
    monkeypatch.setattr(job, "fetch_snapshot", fetch)
    runtime.events.append(event("Another/2026"))
    status, body = run(runtime)
    assert status == 503 and {row["status"] for row in body["events"]} == {"retry"}
    assert len(calls) == 1 and read_before() is None
    assert "private transport" not in json.dumps(body)
    assert runtime.alerts
    stored = json.loads(runtime.s3.objects[job.REPORT_PREFIX + "latest.json"]["Body"])
    assert stored["status"] == "gaps"
    assert run(runtime, now=NOW + timedelta(hours=1))[0] == 200
    assert len(calls) == 2 and read_before() == snapshot()


@pytest.mark.parametrize("field,value", [
    ("effective_at", "2026-11-02T00:00:00Z"),
    ("fetched_at", "2026-11-02T00:00:00Z"),
    ("effective_at", "2026-10-31T12:00:00Z"),
    ("fetched_at", "2026-10-31T12:00:00Z"),
    ("effective_at", "2026-10-05T00:00:00"),
])
def test_late_future_or_undated_snapshot_is_not_saved_as_before(runtime, monkeypatch, field, value):
    async def fetch():
        return snapshot().model_copy(update={field: value})
    monkeypatch.setattr(job, "fetch_snapshot", fetch)
    status, body = run(runtime)
    assert status == 503 and body["events"][0]["error_type"] == "VRSDataError"
    assert read_before() is None


def test_missing_baseline_on_start_day_is_reported_without_a_late_fetch(runtime):
    runtime.events[:] = [event(start="2026-10-31")]
    status, body = run(runtime)
    assert status == 503 and body["events"][0]["status"] == "missed_start"
    assert not runtime.fetches and read_before() is None
    saved_keys = [key for key in runtime.s3.objects if "/events/" in key]
    assert len(saved_keys) == 1
    # The per-event fact survives its removal from the calendar window.
    runtime.events.clear()
    assert run(runtime)[0] == 200
    assert saved_keys[0] in runtime.s3.objects


def test_existing_before_is_valid_on_start_day_but_changed_earlier_date_does_not_overwrite(runtime):
    assert run(runtime)[0] == 200
    assert run(runtime, now=NOW + timedelta(days=2))[1]["events"][0]["status"] == "existing"
    runtime.events[:] = [event(start="2026-10-30")]
    status, body = run(runtime)
    assert status == 503 and body["events"][0]["error_type"] == "VRSDataError"
    assert read_before() == snapshot()
    assert len(runtime.fetches) == 1


def test_concurrent_conditional_create_reloads_the_winning_snapshot(runtime, monkeypatch):
    original = storage.write_vrs_snapshot
    winner = snapshot(version="concurrent-winner")
    async def concurrent(key, phase, value, **kwargs):
        assert await original(key, phase, winner, **kwargs)
        return await original(key, phase, value, **kwargs)
    monkeypatch.setattr(storage, "write_vrs_snapshot", concurrent)
    status, body = run(runtime)
    assert status == 200 and body["events"][0]["status"] == "existing"
    assert body["events"][0]["version"] == "concurrent-winner"
    assert read_before() == winner


def test_partial_or_unavailable_calendar_is_not_reported_as_complete(runtime, monkeypatch):
    runtime.issues.append({"row": 1, "reason": "preview_calendar_invalid"})
    status, body = run(runtime)
    assert status == 503 and body["issues"] == runtime.issues
    assert read_before() == snapshot()  # A bad row does not drop other known events.
    async def unavailable(now, **kwargs):
        raise RuntimeError("private details")
    monkeypatch.setattr(job, "fetch_preview_calendar", unavailable)
    status, body = run(runtime, dry_run=True)
    assert status == 503 and body["status"] == "source_unavailable"
    assert "private details" not in json.dumps(body)


def test_report_failure_is_visible_even_when_snapshot_is_saved(runtime, monkeypatch):
    async def unavailable(body):
        raise storage.StorageUnavailableError("details")
    monkeypatch.setattr(job, "_save_report", unavailable)
    status, body = run(runtime)
    assert status == 503 and body["status"] == "report_unavailable"
    assert read_before() == snapshot() and runtime.alerts


@pytest.mark.parametrize("pool", [100000, "TBD", None])
def test_baseline_calendar_keeps_tier_one_two_lan_without_requiring_announcement_prize_facts(pool):
    row = {"pagename": PAGE, "name": "Future PGL", "startdate": "2026-11-02",
           "enddate": "2026-11-08", "liquipediatier": "2", "type": "Offline", "prizepool": pool}
    assert calendar_source._calendar_event(row, NOW.date(), require_prize_pool=False)["pagename"] == PAGE
    # Editorial calendar requirements retain their original money gate.
    if pool == 100000:
        assert calendar_source._calendar_event(row, NOW.date()) is None


def test_identical_failure_retries_without_repeating_the_owner_alert(runtime, monkeypatch):
    async def unavailable():
        raise VRSUnavailableError("unavailable")
    monkeypatch.setattr(job, "fetch_snapshot", unavailable)
    assert run(runtime)[0] == 503
    assert run(runtime, now=NOW + timedelta(hours=1))[0] == 503
    assert len(runtime.alerts) == 1


def test_unconfirmed_write_never_reports_a_saved_baseline(runtime, monkeypatch):
    async def lost(*args, **kwargs):
        return True
    monkeypatch.setattr(storage, "write_vrs_snapshot", lost)
    status, body = run(runtime)
    assert status == 503 and body["events"][0]["error_type"] == "StorageUnavailableError"
    assert read_before() is None


def test_discovery_captures_baseline_even_when_preview_publication_is_disabled(runtime):
    response = tournament_preview_job.run_preview_job(None, False, runtime.r, now=NOW)
    body = json.loads(response["body"])
    assert body["skipped_reason"] == "disabled" and body["messages_sent"] == 0
    assert body["vrs_baselines"]["events"][0]["status"] == "saved"
    assert read_before() == snapshot()


def test_service_job_needs_no_telegram_pandascore_or_social_credentials(runtime, monkeypatch):
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(main, "CHANNELS", [])
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", "")
    monkeypatch.setattr(main, "LIQUIPEDIA_API_KEY", "test-key")
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setattr(main, "_notify_admin", lambda *args: runtime.alerts.append(args))
    for name in ("publication_delivery_allowed", "instagram_publishing_enabled", "threads_publishing_enabled"):
        monkeypatch.setattr(main, name, lambda *args: pytest.fail("baseline capture cannot depend on social delivery"))
    monkeypatch.setattr(main, "run_vrs_baseline_job", lambda dry, r: job.run_vrs_baseline_job(dry, r, now=NOW))
    assert main.handler({"job": "vrs_baselines"}, None)["statusCode"] == 200
    assert read_before() == snapshot()


def test_disabled_service_job_does_not_require_sources_or_storage(runtime, monkeypatch):
    monkeypatch.setattr(main, "ENABLE_VRS", False)
    monkeypatch.setattr(main, "LIQUIPEDIA_API_KEY", "")
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", "")
    body = json.loads(main.handler({"job": "vrs_baselines"}, None)["body"])
    assert body["status"] == "disabled" and not runtime.fetches and not runtime.s3.objects


def test_preview_reuses_calendar_baseline_without_reading_a_newer_vrs(runtime, monkeypatch):
    assert run(runtime)[0] == 200
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "fetch_vrs_snapshot", lambda: pytest.fail("must reuse the saved before"))
    profile = SimpleNamespace(key="future-pgl", liquipedia_page=PAGE)
    preview = SimpleNamespace(start=datetime(2026, 11, 2).date())
    assert main._capture_preview_vrs_baseline(profile, preview)["status"] == "saved"
    assert asyncio.run(storage.read_vrs_snapshot("event:future-pgl", "before", "initial")) == snapshot()


@pytest.mark.parametrize("legacy", [False, True])
def test_final_with_stage_bridge_finds_calendar_before_and_preserves_legacy_lookup(runtime, monkeypatch, legacy):
    if legacy:
        asyncio.run(storage.write_vrs_snapshot("22228", "before", snapshot()))
    else:
        assert run(runtime)[0] == 200
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    after = snapshot("after", "2026-11-09T00:00:00Z", "2026-11-09T12:00:00Z")
    after.teams[0].points = 1100
    queued = []
    async def fetch():
        return after
    async def enqueue(*args, **kwargs):
        queued.append(kwargs)
        return True
    monkeypatch.setattr(main, "fetch_vrs_snapshot", fetch)
    monkeypatch.setattr(main, "enqueue_result_delivery", enqueue)
    final = MatchNormalized(source="liquipedia", match_id="future-final",
        tournament_name="Future PGL", tournament_parent=PAGE, vrs_baseline_id="22228",
        team1_name="Nemiga", team2_name="FURIA", score1=3, score2=1,
        is_final=True, final_identity_confirmed=True, end_date="2026-11-08T18:00:00Z",
        tournament_placements=[TournamentPlacement(placement="1", team_name="Nemiga"),
                               TournamentPlacement(placement="2", team_name="FURIA")])
    assert main._enqueue_tournament_vrs(final, "global", "global")
    assert queued[0]["vrs_impacts"][0].points_delta == 100
    assert queued[0]["content_type"] == "tournament_vrs_standings"
