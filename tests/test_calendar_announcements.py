"""Offline regression coverage for approved announcements and the sourced annual plan."""
import asyncio
import copy
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from cs2bot import preview_coverage as coverage, tournament_calendar as calendar, tournament_preview_job as job
from cs2bot.match_sources.sources import tournament_preview_source as source
from cs2bot.tournament_preview import (
    PreviewProfile, PreviewUnavailable, check_profile, format_preview_caption,
    format_preview_instagram_caption, format_preview_threads_caption, load_profiles,
)
from cs2bot.tournament_preview_cards import render_preview_cards

ROOT = Path(__file__).parents[1]
REGISTRY = ROOT / "data/tournament_preview_profiles.json"
WINDOWS = [
    ("iem-beijing-2026", "2026-10-31", "2026-11-01", "2026-11-02"),
    ("blast-rivals-hong-kong-2026", "2026-11-09", "2026-11-10", "2026-11-11"),
    ("pgl-major-singapore-2026", "2026-11-22", "2026-11-23", "2026-11-24"),
]


def moment(day):
    return datetime.fromisoformat(day + "T15:00:00+00:00")


def profile(key="pgl-major-singapore-2026"):
    return next(item for item in load_profiles(REGISTRY) if item.key == key)


@pytest.fixture
def api(monkeypatch):
    rows = json.loads((ROOT / "tests/fixtures/preview_calendar_2026.json").read_text())["result"]
    async def fetched(path, params):
        assert path == "/tournament"
        matches = [r for r in rows if params["conditions"] == f"[[pagename::{r['pagename']}]]"]
        return {"result": matches if "[[pagename::" in params["conditions"] else rows}
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    return rows


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from test_tournament_preview import runtime as make_runtime
    r, calls = make_runtime(tmp_path, monkeypatch)
    r.TOURNAMENT_PREVIEW_PROFILES_PATH = REGISTRY
    monkeypatch.setattr(job, "fetch_tournament_preview", source.fetch_tournament_preview)
    alerts = []
    r._notify_admin = lambda *args: alerts.append(args)
    return r, calls, alerts


@pytest.mark.parametrize("key,first,last,start", WINDOWS)
@pytest.mark.parametrize("which", ["first", "last"])
def test_each_registered_event_reaches_delivery_on_both_allowed_days(
        api, runtime, key, first, last, start, which):
    r, calls, alerts = runtime
    response = job.run_preview_job(None, False, r, now=moment(first if which == "first" else last))
    body = json.loads(response["body"])
    assert response["statusCode"] == 200 and body["messages_sent"] == 1
    assert [item["key"] for item in body["previews"]] == [key]
    assert body["previews"][0]["deliveries"]["telegram:c"]["status"] == "sent"
    assert body["coverage"]["status"] == "ok" and alerts == [] and calls.count("send") == 1


@pytest.mark.parametrize("key,first,last,start", WINDOWS)
def test_start_day_is_never_a_late_announcement(api, runtime, key, first, last, start):
    r, calls, _ = runtime
    body = json.loads(job.run_preview_job(None, False, r, now=moment(start))["body"])
    assert body["messages_sent"] == 0 and body["previews"] == [] and "send" not in calls


@pytest.mark.parametrize("key,first,last,start", WINDOWS)
def test_all_real_profiles_fit_three_platforms_and_square_cover(api, key, first, last, start):
    p = profile(key)
    preview = asyncio.run(source.fetch_tournament_preview(p, now=moment(first)))
    assert len(format_preview_caption(preview)) <= 1024
    assert len(format_preview_instagram_caption(preview)) <= 2200
    text = format_preview_threads_caption(preview)
    assert len(text.encode("utf-16-le")) // 2 <= 500
    cards = render_preview_cards(preview)
    assert len(cards) == 1 and Image.open(io.BytesIO(cards[0])).size == (1080, 1080)
    assert not preview.teams and p.branding.logo_asset
    if key.startswith("pgl-major"):
        assert [stage.kind for stage in preview.stages] == ["swiss_1", "swiss_2", "swiss_3", "playoffs"]
        assert "Swiss 1–3:" in text and "28 ноября – 1 декабря" in text
        preview.stages[2].format_text = "Все матчи BO3; 16→8."
        assert "Swiss 1–3:" not in format_preview_threads_caption(preview)


def test_scheduled_approval_is_real_dated_bounded_and_legacy_review_still_expires():
    p = profile()
    check_profile(p, moment("2026-11-23"))
    assert p.verified_at < moment("2026-10-25")
    with pytest.raises(PreviewUnavailable, match="editorial_review_stale"):
        check_profile(p, moment("2026-11-24"))
    old = load_profiles(REGISTRY)[0]
    with pytest.raises(PreviewUnavailable, match="editorial_review_stale"):
        check_profile(old, old.verified_at + timedelta(days=8))
    p.approved = False
    with pytest.raises(PreviewUnavailable, match="profile_not_approved"):
        check_profile(p, moment("2026-11-22"))


@pytest.mark.parametrize("changes", [
    {"publication_approval_until": "2026-11-25T00:00:00Z"},
    {"publication_approval_until": "2026-11-23T00:00:00"},
    {"publication_approval_until": "2026-10-01T00:00:00Z"},
    {"verified_at": "2026-08-01T00:00:00Z"},
    {"expected_prize_pool_usd": None}, {"expected_participant_count": None},
])
def test_scoped_approval_cannot_remove_fact_or_time_bounds(changes):
    with pytest.raises(ValidationError):
        PreviewProfile.model_validate({**profile().model_dump(), **changes})


@pytest.mark.parametrize("changes,reason", [
    ({"prizepool": "1000000"}, "reviewed_prize_pool_conflict"),
    ({"participantsnumber": 24}, "reviewed_participant_count_conflict"),
    ({"participantsnumber": None}, "reviewed_participant_count_conflict"),
    ({"startdate": "2026-11-25"}, "reviewed_tournament_dates_conflict"),
    ({"enddate": "2026-12-14"}, "reviewed_tournament_dates_conflict"),
])
def test_changed_live_passport_still_blocks_the_approved_profile(api, changes, reason):
    api[-1].update(changes)
    with pytest.raises(PreviewUnavailable, match=reason):
        asyncio.run(source.fetch_tournament_preview(profile(), now=moment("2026-11-22")))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered", "overlap"])
def test_major_stages_cannot_be_lost_duplicated_or_reordered(mutation):
    data = copy.deepcopy(profile().model_dump())
    if mutation == "missing":
        del data["stages"][1]
    elif mutation == "duplicate":
        data["stages"][1]["kind"] = "swiss_1"
    elif mutation == "reordered":
        data["stages"][0]["kind"], data["stages"][1]["kind"] = "swiss_2", "swiss_1"
    else:
        data["stages"][1]["start"] = data["stages"][0]["start"]
    with pytest.raises(ValidationError):
        PreviewProfile.model_validate(data)


def test_dry_run_never_alerts_claims_or_delivers(api, runtime):
    r, calls, alerts = runtime
    body = json.loads(job.run_preview_job(None, True, r, now=moment("2026-10-31"))["body"])
    assert body["previews"][0]["eligible"] and body["previews"][0]["caption_errors"] == {}
    assert calls == [] and alerts == []


def test_missing_pgl_profile_is_visible_even_when_it_is_liquipedia_a_tier(api, runtime):
    r, _, alerts = runtime
    api[:] = [{**api[-1], "pagename": "PGL/2026/Masters", "name": "PGL Masters Bucharest 2026",
               "startdate": "2026-10-24", "enddate": "2026-10-31", "liquipediatier": "2"}]
    body = json.loads(job.run_preview_job(None, False, r, now=moment("2026-10-22"))["body"])
    assert body["previews"] == [] and body["messages_sent"] == 0
    assert body["coverage"]["issues"][0]["reason"] == "preview_profile_missing"
    assert any("PGL Masters Bucharest" in message for code, message in alerts)


def test_calendar_outage_does_not_block_ready_announcement(api, runtime, monkeypatch):
    async def failed(*args):
        raise RuntimeError("arbitrary exception text must not be exposed")
    monkeypatch.setattr(coverage, "fetch_preview_calendar", failed)
    r, calls, alerts = runtime
    body = json.loads(job.run_preview_job(None, False, r, now=moment("2026-10-31"))["body"])
    assert body["coverage"] == {"status": "unavailable", "reason": "RuntimeError", "issues": []}
    assert body["messages_sent"] == 1 and calls.count("send") == 1 and len(alerts) == 1


def test_preparation_failure_is_alerted_but_dry_run_is_read_only(api, runtime):
    r, _, alerts = runtime
    api[0]["participantsnumber"] = 2
    body = json.loads(job.run_preview_job("iem-beijing-2026", False, r, now=moment("2026-10-31"))["body"])
    assert body["previews"][0]["skipped_reason"] == "reviewed_participant_count_conflict"
    assert len(alerts) == 1
    alerts.clear()
    job.run_preview_job("iem-beijing-2026", True, r, now=moment("2026-10-31"))
    assert alerts == []


@pytest.mark.parametrize("changes", [
    {"liquipediatier": "3"}, {"type": "Online"}, {"liquipediatiertype": "Qualifier"},
    {"liquipediatiertype": "Points"}, {"status": "cancelled"}, {"prizepool": 499999},
    {"startdate": "2026-10-21"}, {"startdate": "2026-10-30"},
])
def test_live_calendar_excludes_ineligible_events(api, changes):
    api[:] = [{**api[0], "startdate": "2026-10-24", **changes}]
    assert asyncio.run(source.fetch_preview_calendar(moment("2026-10-22"))) == []


@pytest.mark.parametrize("field", ["type", "liquipediatier"])
def test_invalid_calendar_never_claims_healthy_coverage(api, field):
    api[:] = [{**api[0], "startdate": "2026-10-24"}]
    del api[0][field]
    with pytest.raises(PreviewUnavailable, match="preview_calendar_invalid"):
        asyncio.run(source.fetch_preview_calendar(moment("2026-10-22")))


def test_live_calendar_refuses_truncated_pagination(api, monkeypatch):
    api[:] = [{**api[0], "startdate": "2026-10-24"}]
    monkeypatch.setattr(source, "CALENDAR_PAGE_SIZE", 1)
    monkeypatch.setattr(source, "CALENDAR_MAX_PAGES", 2)
    with pytest.raises(PreviewUnavailable, match="pagination_incomplete"):
        asyncio.run(source.fetch_preview_calendar(moment("2026-10-22")))


def test_disabled_discovery_has_no_calendar_access(runtime, monkeypatch):
    r, calls, _ = runtime
    r.ENABLE_TOURNAMENT_PREVIEWS = False
    monkeypatch.setattr(job, "audit_calendar_plan", lambda *a, **k: pytest.fail("disabled job scanned plan"))
    body = json.loads(job.run_preview_job(None, False, r)["body"])
    assert body["skipped_reason"] == "disabled" and calls == []


def test_annual_plan_has_28_announced_events_and_does_not_forge_approval():
    entries = calendar.load_calendar()
    planned = [e for e in entries if (e.start.year if e.start else int(e.month[:4])) == 2027]
    assert len(entries) == 31 and len(planned) == 28
    assert not any("qualifier" in e.key or "awards" in e.key for e in entries)
    assert {e.key for e in planned}.isdisjoint({p.key for p in load_profiles(REGISTRY)})
    by_key = {e.key: e for e in entries}
    pgl = by_key["pgl-bucharest-2027"]
    assert pgl.start.isoformat() == "2027-01-15" and pgl.prize_pool_usd == 1400000
    assert pgl.alternate_dates[0].start.isoformat() == "2027-01-17"
    enc = by_key["esports-nations-cup-2027"]
    assert enc.month == "2027-11" and enc.start is None and enc.end is None and enc.prize_pool_usd is None


@pytest.fixture
def one_plan(tmp_path):
    path = tmp_path / "calendar.json"
    row = next(e.model_dump(mode="json") for e in calendar.load_calendar()
               if e.key == "blast-bounty-season-1-2027")
    path.write_text(json.dumps([row]))
    alerts = []
    return path, row, alerts


@pytest.mark.parametrize("day,warns", [
    ("2026-12-30", False), ("2026-12-31", True), ("2027-01-12", True), ("2027-01-14", False)])
def test_plan_warns_from_fourteen_days_and_does_not_assume_a_missing_post_after_start(one_plan, day, warns):
    from types import SimpleNamespace
    path, _, alerts = one_plan
    r = SimpleNamespace(TOURNAMENT_PREVIEW_CALENDAR_PATH=path, _notify_admin=lambda *a: alerts.append(a))
    result = calendar.audit_calendar_plan([], moment(day), r, dry_run=False)
    assert bool(alerts) is warns and bool(result["issues"]) is warns


def test_month_only_date_is_never_replaced_with_a_placeholder_day(one_plan):
    from types import SimpleNamespace
    path, row, alerts = one_plan
    row.update(start=None, end=None, month="2027-11", date_status="month_only")
    path.write_text(json.dumps([row]))
    r = SimpleNamespace(TOURNAMENT_PREVIEW_CALENDAR_PATH=path, _notify_admin=lambda *a: alerts.append(a))
    result = calendar.audit_calendar_plan([], moment("2027-10-18"), r, dry_run=True)
    assert result["issues"][0]["status"] == "exact_dates_missing"
    assert result["issues"][0]["start"] is None and alerts == []


@pytest.mark.parametrize("changes", [
    {"end": "2026-01-01"}, {"end": None}, {"start": None, "end": None},
    {"date_status": "conflict"}, {"month": "2027-13"}, {"prize_pool_usd": True},
    {"source_urls": []}, {"provider_id": "invented"},
])
def test_plan_rejects_incomplete_or_invented_metadata(one_plan, changes):
    path, row, _ = one_plan
    row.update(changes)
    path.write_text(json.dumps([row]))
    with pytest.raises(ValueError):
        calendar.load_calendar(path)


def test_planned_event_stays_visible_when_live_api_calendar_is_empty(api, runtime, one_plan):
    r, calls, _ = runtime
    path, _, alerts = one_plan
    r.TOURNAMENT_PREVIEW_CALENDAR_PATH = path
    r._notify_admin = lambda *a: alerts.append(a)
    api.clear()
    body = json.loads(job.run_preview_job(None, False, r, now=moment("2027-01-01"))["body"])
    assert body["coverage"]["events_checked"] == 0
    assert body["calendar_plan"]["issues"][0]["key"] == "blast-bounty-season-1-2027"
    assert len(alerts) == 1 and body["messages_sent"] == 0 and "send" not in calls


@pytest.mark.parametrize("problem", ["dates_conflict", "profile_facts_conflict"])
def test_automatic_preview_never_ignores_a_known_plan_conflict(api, runtime, tmp_path, problem):
    r, calls, alerts = runtime
    row = next(e.model_dump(mode="json") for e in calendar.load_calendar()
               if e.key == "iem-beijing-2026")
    if problem == "dates_conflict":
        row.update(date_status="conflict", alternate_dates=[{
            "start": "2026-11-03", "end": "2026-11-08", "source_url": "https://example.org/update"}])
    else:
        row["prize_pool_usd"] = 1500000
    path = tmp_path / "changed-plan.json"
    path.write_text(json.dumps([row]))
    r.TOURNAMENT_PREVIEW_CALENDAR_PATH = path
    body = json.loads(job.run_preview_job(None, False, r, now=moment("2026-10-31"))["body"])
    assert body["previews"][0]["skipped_reason"] == "calendar_" + problem
    assert body["messages_sent"] == 0 and "send" not in calls and alerts
