import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from cs2bot import main, tournament_publications as jobs
from cs2bot.match_sources import storage
from cs2bot.match_sources.models import VRSRankingSnapshot, VRSTeamSnapshot
from cs2bot.match_sources.sources.tournament_preview_source import PreviewCalendarScan
from cs2bot.match_sources.sources.tournament_preview_source import _calendar_event
from cs2bot.tournament_final_delivery import deliver_final
from test_beijing_final_recovery import beijing_pair, PAGE
from test_main import configured_runtime
from test_storage import FakeS3


NOW = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)


@pytest.fixture
def lifecycle(monkeypatch, configured_runtime, beijing_pair):
    primary, final = beijing_pair
    s3, notices, sends = FakeS3(), [], []
    snapshots = {}
    monkeypatch.setattr(storage, "_client", lambda: s3)
    monkeypatch.setattr(storage, "_bucket", lambda: "test")
    for name in ("reconcile_channel_delivery", "reconcile_content_delivery", "mark_delivery_claim_attempting",
                 "mark_delivery_claim_sent", "mark_delivery_claim_uncertain", "enqueue_result_delivery",
                 "list_pending_result_deliveries", "record_result_delivery_attempt", "delete_result_delivery",
                 "is_channel_processed", "reserve_threads_chain_append", "confirm_threads_chain_append",
                 "release_threads_chain_append", "block_threads_chain_append"):
        monkeypatch.setattr(main, name, getattr(storage, name))
    monkeypatch.setattr(main, "ENABLE_TOURNAMENT_PUBLICATIONS", True)
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "@test"}])
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    monkeypatch.setattr(main, "_delivery_is_current", lambda *args: True)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: notices.append(args))
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "render_final_card", lambda *args, **kwargs: b"png")
    monkeypatch.setattr(main, "send_photo_to_telegram", lambda *args, **kwargs: sends.append(args))

    def snapshot(version, at, bonus=0):
        return VRSRankingSnapshot(source="Valve VRS", version=version, effective_at=at, fetched_at=at,
            teams=[VRSTeamSnapshot(team_id=str(i), team_name=p.team_name, rank=i,
                                   points=1000 - i * 100 + bonus)
                   for i, p in enumerate(final.tournament_placements, 1)])

    snapshots["after"] = snapshot("after", "2026-11-09T10:00:00Z", 100)
    asyncio.run(storage.write_vrs_snapshot("event:iem-beijing-2026", "before",
        snapshot("before", "2026-10-31T10:00:00Z"), initial=True))

    async def fetch_vrs():
        return snapshots["after"]

    async def fetch_matches(*args, **kwargs):
        assert kwargs["parent"] == PAGE
        return [final.model_copy(deep=True)]

    async def calendar(*args, **kwargs):
        return PreviewCalendarScan([], [])

    monkeypatch.setattr(main, "fetch_vrs_snapshot", fetch_vrs)
    monkeypatch.setattr(jobs, "fetch_finished_matches", fetch_matches)
    monkeypatch.setattr(jobs, "fetch_preview_calendar", calendar)
    record = jobs.register_event({"pagename": PAGE, "name": "Beijing", "start": "2026-11-02",
        "end": "2026-11-08", "participant_count": 4}, main, now=NOW - timedelta(days=8))
    return s3, primary, final, record, notices, sends, snapshots


def run(now=NOW, **kwargs):
    response = jobs.run_publications_job(False, main, recovery_key="iem-beijing-2026", now=now, **kwargs)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


def tasks(body):
    return {t["kind"]: t for t in body["events"][0]["tasks"]}


def test_late_source_retries_without_a_recent_match_and_after_ordinary_result(monkeypatch, lifecycle):
    s3, primary, final, record, notices, sends, _ = lifecycle
    asyncio.run(storage.mark_channel_processed(primary, "global"))

    async def down(*args, **kwargs):
        raise OSError("source offline")

    monkeypatch.setattr(jobs, "fetch_finished_matches", down)
    first = run()
    assert all(t["status"] == "waiting" for t in tasks(first).values())
    assert not asyncio.run(storage.list_pending_result_deliveries())
    assert notices

    async def recovered(*args, **kwargs):
        return [final]

    monkeypatch.setattr(jobs, "fetch_finished_matches", recovered)
    second = run(NOW + timedelta(days=4))
    assert {t["status"] for t in tasks(second).values()} == {"queued"}
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 3
    assert sends == []  # Preparation never sends public content.


def test_source_http_status_is_visible_without_exposing_remote_error_text(monkeypatch, lifecycle):
    async def failed(*args, **kwargs):
        raise jobs.SourceUnavailableError("Liquipedia returned HTTP 404; sensitive remote detail")

    monkeypatch.setattr(jobs, "fetch_finished_matches", failed)
    result = tasks(run())
    assert {t["reason"] for t in result.values()} == {"final_source:SourceUnavailableError:HTTP_404"}


def test_final_and_vrs_do_not_wait_for_cash_payouts(monkeypatch, lifecycle):
    _, _, final, _, _, _, _ = lifecycle
    final.winner_prize_usd = None
    for placement in final.tournament_placements:
        placement.prize_usd = None
    result = tasks(run())
    assert result["tournament_final"]["status"] == "queued"
    assert result["tournament_standings"]["reason"] == "awaiting_complete_payouts"
    assert result["tournament_vrs_standings"]["status"] == "queued"
    for placement in final.tournament_placements:
        placement.prize_usd = 100
    result = tasks(run(NOW + timedelta(hours=1)))
    assert result["tournament_standings"]["status"] == "queued"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 3


def test_after_snapshot_is_retried_without_blocking_final_or_prizes(lifecycle):
    *_, snapshots = lifecycle
    snapshots["after"].effective_at = "2026-11-08T08:00:00Z"
    result = tasks(run())
    assert result["tournament_final"]["status"] == result["tournament_standings"]["status"] == "queued"
    assert result["tournament_vrs_standings"]["reason"] == "awaiting_after_snapshot"
    snapshots["after"].effective_at = "2026-11-09T10:00:00Z"
    assert tasks(run(NOW + timedelta(hours=1)))["tournament_vrs_standings"]["status"] == "queued"


def test_registry_uses_persistent_processed_markers_without_reopening_sent_posts(lifecycle):
    _, _, _, record, _, _, _ = lifecycle
    for task in record.tasks:
        asyncio.run(storage.mark_content_processed(task.uid, task.kind))
    assert all(t["status"] == "sent" for t in tasks(run()).values())
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_dry_run_does_not_write_registry_outbox_snapshots_or_reports(lifecycle):
    s3 = lifecycle[0]
    before = dict(s3.objects)
    response = jobs.run_publications_job(True, main, recovery_key="iem-beijing-2026", now=NOW)
    assert response["statusCode"] == 200
    assert s3.objects == before
    assert {t["reason"] for t in tasks(json.loads(response["body"])).values()} == {"would_enqueue"}


def test_disabled_generation_does_not_revive_when_policy_is_reenabled(monkeypatch, lifecycle):
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (False, 1))
    assert all(t["status"] == "disabled" for t in tasks(run()).values())
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 1))
    assert all(t["status"] == "disabled" for t in tasks(run(NOW + timedelta(hours=1))).values())
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_old_regular_result_does_not_suppress_final_and_second_delivery_is_reconciled(lifecycle):
    _, primary, _, _, _, sends, _ = lifecycle
    asyncio.run(storage.mark_channel_processed(primary, "global"))
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_final")
    channel = {"chat_id": "@test"}
    assert deliver_final(pending, channel, main) == "sent"
    assert deliver_final(pending, channel, main) == "reconciled"
    assert len(sends) == 1
    assert tasks(run(NOW + timedelta(minutes=15)))["tournament_final"]["status"] == "sent"


@pytest.mark.parametrize("platform", ["instagram", "threads"])
def test_social_final_delivery_uses_its_own_claim_and_reconciles_once(monkeypatch, lifecycle, platform):
    sends = []
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_final")
    pending = replace(pending, channel_id=platform,
                      content_uid=jobs.final_content_uid(pending.match, platform))
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a, **kw: sends.append((a, kw)))
    monkeypatch.setattr(main, "_publish_threads_chain", lambda *a, **kw: sends.append((a, kw)))
    channel = {"id": platform, "platform": platform}
    assert deliver_final(pending, channel, main) == "sent"
    assert deliver_final(pending, channel, main) == "reconciled"
    assert len(sends) == 1
    if platform == "threads":
        assert sends[0][1]["allow_missing_root"] is True


def test_uncertain_send_is_retained_for_review_and_never_repeated(monkeypatch, lifecycle):
    *_, sends, _ = lifecycle
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_final")

    def uncertain(*args, **kwargs):
        sends.append(args)
        raise main.TelegramDeliveryUncertainError("outcome unknown")

    monkeypatch.setattr(main, "send_photo_to_telegram", uncertain)
    assert deliver_final(pending, {"chat_id": "@test"}, main) == "uncertain"
    assert deliver_final(pending, {"chat_id": "@test"}, main) == "duplicate"
    assert len(sends) == 1
    assert tasks(run(NOW + timedelta(hours=2)))["tournament_final"]["status"] == "review"


def test_confirmed_send_survives_processed_write_failure(monkeypatch, lifecycle):
    _, _, _, _, _, sends, _ = lifecycle
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_final")
    real_mark = main.mark_content_processed

    async def fail(*args, **kwargs):
        raise storage.StorageUnavailableError("marker unavailable")

    monkeypatch.setattr(main, "mark_content_processed", fail)
    assert deliver_final(pending, {"chat_id": "@test"}, main) == "failed"
    monkeypatch.setattr(main, "mark_content_processed", real_mark)
    assert deliver_final(pending, {"chat_id": "@test"}, main) == "reconciled"
    assert len(sends) == 1


def test_threads_can_create_a_final_root_when_preview_was_absent(lifecycle):
    s3 = lifecycle[0]
    append = asyncio.run(storage.reserve_threads_chain_append("threads:event:test", require_root=True, allow_missing_root=True))
    assert append.is_root and append.reply_to_id is None
    asyncio.run(storage.confirm_threads_chain_append(append, "final-post"))
    next_append = asyncio.run(storage.reserve_threads_chain_append("threads:event:test", require_root=True))
    assert next_append.reply_to_id == "final-post" and not next_append.is_root


def test_registry_conflict_does_not_overwrite_newer_state(lifecycle):
    record = lifecycle[3]
    store = jobs.Store()
    key = jobs.event_storage_key(record.page)
    payload, etag = store.read(key)
    store.write(key, payload, etag)
    with pytest.raises(storage.StorageUnavailableError):
        store.write(key, {**payload, "name": "stale"}, etag)
    assert store.read(key)[0]["name"] != "stale"


def test_calendar_registration_does_not_require_a_reviewed_profile(monkeypatch, lifecycle):
    row = {"pagename": "Independent/2026", "name": "Independent", "prize_pool_usd": 1000000,
           "start": "2026-11-12", "end": "2026-11-15"}

    async def scan(*args, **kwargs):
        return PreviewCalendarScan([row], [])

    monkeypatch.setattr(jobs, "fetch_preview_calendar", scan)
    response = jobs.run_publications_job(False, main, now=NOW)
    assert response["statusCode"] == 200
    record, _ = jobs.Store().read(jobs.event_storage_key(row["pagename"]))
    assert record["event_id"] == row["pagename"]
    assert len(record["tasks"]) == 3


def test_calendar_scope_captures_tier2_before_without_publishing_it(monkeypatch, lifecycle):
    row = {"pagename": "Small/2026", "name": "Small regional cup", "prize_pool_usd": 10000,
           "start": "2026-11-12", "end": "2026-11-15"}

    async def scan(*args, **kwargs):
        return PreviewCalendarScan([row], [])

    monkeypatch.setattr(jobs, "fetch_preview_calendar", scan)
    response = jobs.run_publications_job(False, main, now=NOW)
    assert response["statusCode"] == 200
    record, _ = jobs.Store().read(jobs.event_storage_key(row["pagename"]))
    assert record["tasks"] == [] and record["editorial_admitted"] is False
    baseline = asyncio.run(storage.read_vrs_snapshot(jobs.calendar_baseline_id(row["pagename"]), "before", "initial"))
    assert baseline is not None
    assert {p.match.tournament_parent for p in asyncio.run(storage.list_pending_result_deliveries())} == {PAGE}


def test_calendar_can_admit_a_previously_baseline_only_event_after_verified_pool(monkeypatch, lifecycle):
    row = {"pagename": "Independent/2026", "name": "Independent", "start": "2026-11-12", "end": "2026-11-15"}
    assert jobs.register_event(row, main, now=NOW).tasks == []

    async def scan(*args, **kwargs):
        return PreviewCalendarScan([{**row, "prize_pool_usd": 1000000}], [])

    monkeypatch.setattr(jobs, "fetch_preview_calendar", scan)
    assert jobs.run_publications_job(False, main, now=NOW)["statusCode"] == 200
    record, _ = jobs.Store().read(jobs.event_storage_key(row["pagename"]))
    assert record["editorial_admitted"] is True
    assert len(record["tasks"]) == 3


@pytest.mark.parametrize("change", [{"is_lan": False}, {"is_lan": None, "location": "Online"}])
def test_registered_major_does_not_override_final_hard_exclusions(lifecycle, change):
    final = lifecycle[2]
    for key, value in change.items():
        setattr(final, key, value)
    result = tasks(run())
    assert {task["status"] for task in result.values()} == {"disabled"}
    assert all(task["reason"].startswith("outside_editorial_scope:") for task in result.values())
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_worker_route_accepts_only_a_known_recovery_profile(monkeypatch, lifecycle):
    assert main.handler({"job": "tournament_publications", "recovery_profile_key": "missing", "dry_run": True}, None)["statusCode"] == 400
    assert main.handler({"job": "tournament_publications", "recovery_profile_key": "../bad", "dry_run": True}, None)["statusCode"] == 400


def test_rollout_does_not_backfill_finished_calendar_history(monkeypatch, lifecycle):
    row = {"pagename": "Old/2026", "name": "Old", "start": "2026-11-01", "end": "2026-11-08"}

    async def scan(*args, **kwargs):
        return PreviewCalendarScan([row], [])

    monkeypatch.setattr(main, "TOURNAMENT_PUBLICATIONS_START_DATE", "2026-11-10")
    monkeypatch.setattr(jobs, "fetch_preview_calendar", scan)
    response = jobs.run_publications_job(False, main, now=NOW)
    assert response["statusCode"] == 200
    assert jobs.Store().read(jobs.event_storage_key(row["pagename"]))[0] is None
    # The already registered Beijing obligation survives the rollout cutoff.
    assert json.loads(response["body"])["events"][0]["page"] == PAGE


def test_calendar_lookback_includes_recently_completed_tournaments():
    row = {"pagename": PAGE, "name": "Beijing", "startdate": "2026-11-02",
           "enddate": "2026-11-08", "liquipediatier": "1", "type": "Offline",
           "participantsnumber": "16"}
    result = _calendar_event(row, date(2026, 11, 10), require_prize_pool=False, lookback_days=14)
    assert result["pagename"] == PAGE
    assert result["participant_count"] == 16


def test_policy_outage_is_retryable_and_does_not_disable_tasks(monkeypatch, lifecycle):
    s3 = lifecycle[0]
    before = dict(s3.objects)

    async def unavailable():
        raise storage.StorageUnavailableError("policy temporarily unavailable")

    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    monkeypatch.setattr(main, "read_policy", unavailable)
    response = jobs.run_publications_job(False, main, recovery_key="iem-beijing-2026", now=NOW)
    assert response["statusCode"] == 503
    assert s3.objects == before


def test_vrs_delivery_keeps_registered_identity_when_profile_mapping_changes(monkeypatch, lifecycle):
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_vrs_standings")
    original_uid = pending.content_uid
    updated = asyncio.run(storage.record_result_delivery_attempt(pending))
    assert updated.content_uid == original_uid
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_vrs_standings")
    monkeypatch.setattr(main, "_vrs_tournament_id", lambda match: "event:new-profile-mapping")
    monkeypatch.setattr(main, "render_tournament_vrs_cards", lambda *args, **kwargs: [b"png"])
    assert main._deliver_tournament_vrs(pending, {"chat_id": "@test"}, "global") == "sent"
    assert asyncio.run(storage.is_processed("content_" + storage.safe_storage_part(original_uid)))
    assert tasks(run(NOW + timedelta(minutes=15)))["tournament_vrs_standings"]["status"] == "sent"


def test_prize_card_render_failure_is_retried_without_text_only_completion(monkeypatch, lifecycle):
    sends = lifecycle[5]
    run()
    pending = next(p for p in asyncio.run(storage.list_pending_result_deliveries()) if p.content_type == "tournament_standings")

    def failed_render(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(main, "render_tournament_standings_cards", failed_render)
    monkeypatch.setattr(main, "send_to_telegram", lambda *a, **k: pytest.fail("text fallback closed the card task"))
    assert main._deliver_tournament_standings(pending, {"chat_id": "@test"}, "global") == "failed"
    assert sends == []
    assert not asyncio.run(storage.is_processed("content_" + storage.safe_storage_part(pending.content_uid)))
    monkeypatch.setattr(main, "render_tournament_standings_cards", lambda *a, **k: [b"png"])
    assert main._deliver_tournament_standings(pending, {"chat_id": "@test"}, "global") == "sent"
    assert len(sends) == 1


def test_budget_report_keeps_all_expected_events(lifecycle):
    class Context:
        def get_remaining_time_in_millis(self):
            return 5000

    response = jobs.run_publications_job(False, main, recovery_key="iem-beijing-2026", now=NOW, context=Context())
    body = json.loads(response["body"])
    assert response["statusCode"] == 503
    assert len(body["events"]) == 1
    assert body["issues"][0]["stage"] == "budget"
