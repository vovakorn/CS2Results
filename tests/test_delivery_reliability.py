import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources import storage
from cs2bot.media_cards import MediaCardError
from test_main import configured_runtime, _async, _match, _upcoming
from test_storage import FakeS3


@pytest.fixture
def delivery_storage(monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda: s3)
    monkeypatch.setattr(storage, "_bucket", lambda: "test-bucket")
    # Exercise the actual conditional state transitions rather than the no-op
    # transitions used by the general handler fixture.
    for name in (
        "claim_content_delivery", "reconcile_content_delivery",
        "mark_delivery_claim_attempting", "mark_delivery_claim_sent",
        "mark_delivery_claim_uncertain", "mark_content_processed",
        "release_delivery_claim", "record_result_delivery_attempt",
        "delete_result_delivery",
    ):
        monkeypatch.setattr(main, name, getattr(storage, name))
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)

    def unexpected_request(*args, **kwargs):
        pytest.fail("Reliability tests must not make external requests")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected_request)
    return s3


@pytest.fixture
def vrs_delivery(monkeypatch, delivery_storage):
    impact = main.TournamentVRSImpact(
        placement="1", team_name="NAVI", team_id="navi",
        before_points=100, after_points=120, before_rank=3, after_rank=2,
        points_delta=20, rank_delta=1, source="Valve VRS",
        before_version="before", after_version="after",
    )
    other = impact.model_copy(update={"placement": "2", "team_name": "MOUZ", "team_id": "mouz"})
    match = _match().model_copy(update={"vrs_baseline_id": "reliability-test"})
    asyncio.run(storage.enqueue_result_delivery(
        match, "global", "global", content_type="tournament_vrs_standings",
        vrs_impacts=(impact, other),
    ))
    pending = asyncio.run(storage.list_pending_result_deliveries())[0]
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "branding_for_match", lambda *args: None)
    monkeypatch.setattr(main, "render_tournament_vrs_cards", lambda *args, **kwargs: [b"one", b"two"])
    notices = []
    monkeypatch.setattr(main, "_notify_admin", lambda *args: notices.append(args))
    return pending, notices


def _deliver_vrs(pending):
    return main._deliver_tournament_vrs(pending, {"chat_id": "@test"}, "global")


def _vrs_claim_state(s3):
    return next(value["Metadata"]["delivery-state"] for key, value in s3.objects.items()
                if key.startswith("claims/"))


@pytest.mark.parametrize("media", ["album", "photo", "text"])
def test_vrs_unknown_delivery_never_falls_back_or_sends_again(monkeypatch, delivery_storage, vrs_delivery, media):
    pending, notices = vrs_delivery
    cards = {"album": [b"one", b"two"], "photo": [b"one"], "text": []}[media]
    monkeypatch.setattr(main, "render_tournament_vrs_cards", lambda *args, **kwargs: cards)
    sent = []

    def unknown(kind):
        def send(*args, **kwargs):
            sent.append(kind)
            raise main.TelegramDeliveryUncertainError("simulated unknown delivery")
        return send

    monkeypatch.setattr(main, "send_media_group_to_telegram", unknown("album"))
    monkeypatch.setattr(main, "send_photo_to_telegram", unknown("photo"))
    monkeypatch.setattr(main, "send_to_telegram", unknown("text"))

    assert _deliver_vrs(pending) == "uncertain"
    assert _deliver_vrs(pending) == "duplicate"
    assert sent == [media]
    assert _vrs_claim_state(delivery_storage) == "uncertain"
    assert asyncio.run(storage.list_pending_result_deliveries()) == []
    assert notices[0][0] == "vrs_delivery_uncertain"


@pytest.mark.parametrize("failed_write", ["uncertain", "outbox"])
def test_vrs_unknown_delivery_stays_blocked_when_cleanup_fails(
    monkeypatch, delivery_storage, vrs_delivery, failed_write,
):
    pending, notices = vrs_delivery
    sent = []

    def unknown(*args, **kwargs):
        sent.append("album")
        raise main.TelegramDeliveryUncertainError("simulated unknown delivery")

    async def unavailable(*args):
        raise storage.StorageUnavailableError("simulated state failure")

    monkeypatch.setattr(main, "send_media_group_to_telegram", unknown)
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: pytest.fail("Unsafe text fallback"))
    if failed_write == "uncertain":
        monkeypatch.setattr(main, "mark_delivery_claim_uncertain", unavailable)
    else:
        monkeypatch.setattr(main, "delete_result_delivery", unavailable)

    assert _deliver_vrs(pending) == "uncertain"
    assert _deliver_vrs(pending) == "duplicate"
    assert sent == ["album"]
    assert _vrs_claim_state(delivery_storage) == ("attempting" if failed_write == "uncertain" else "uncertain")
    queue = asyncio.run(storage.list_pending_result_deliveries())
    assert len(queue) == (0 if failed_write == "uncertain" else 1)
    assert all(item.attempt_count == 0 for item in queue)
    assert notices[0][0] == "vrs_delivery_uncertain"


def test_vrs_render_failure_delivers_text_and_finishes_queue(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    def broken_renderer(*args, **kwargs):
        raise MediaCardError("simulated missing asset")

    monkeypatch.setattr(main, "render_tournament_vrs_cards", broken_renderer)
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent.append(args[1]))
    assert _deliver_vrs(pending) == "sent"
    assert _deliver_vrs(pending) == "duplicate"
    assert len(sent) == 1 and "NAVI" in sent[0]
    assert _vrs_claim_state(delivery_storage) == "sent"
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_vrs_definite_failure_keeps_outbox_and_can_retry(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    def rejected(*args, **kwargs):
        raise main.TelegramDeliveryError("simulated definite rejection")

    monkeypatch.setattr(main, "send_media_group_to_telegram", rejected)
    monkeypatch.setattr(main, "send_to_telegram", rejected)
    assert _deliver_vrs(pending) == "failed"
    assert _vrs_claim_state(delivery_storage) == "released"
    queue = asyncio.run(storage.list_pending_result_deliveries())
    assert len(queue) == 1 and queue[0].attempt_count == 1

    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent.append(args[1]))
    assert _deliver_vrs(queue[0]) == "sent"
    assert len(sent) == 1
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_vrs_sent_state_write_failure_never_reopens_delivery(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    async def unavailable(*args):
        raise storage.StorageUnavailableError("simulated sent-state failure")

    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: sent.append("album"))
    monkeypatch.setattr(main, "mark_delivery_claim_sent", unavailable)
    assert _deliver_vrs(pending) == "failed"
    assert _deliver_vrs(pending) == "duplicate"
    assert sent == ["album"]
    assert _vrs_claim_state(delivery_storage) == "attempting"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1


def test_vrs_unexpected_send_error_is_contained_without_reopening_claim(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    def unexpected(*args, **kwargs):
        sent.append("album")
        raise RuntimeError("simulated unexpected sender failure")

    monkeypatch.setattr(main, "send_media_group_to_telegram", unexpected)
    assert _deliver_vrs(pending) == "failed"
    assert _deliver_vrs(pending) == "duplicate"
    assert sent == ["album"]
    assert _vrs_claim_state(delivery_storage) == "attempting"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1


def test_vrs_processed_marker_failure_reconciles_without_resending(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    async def unavailable(*args):
        raise storage.StorageUnavailableError("simulated marker failure")

    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: sent.append("album"))
    monkeypatch.setattr(main, "mark_content_processed", unavailable)
    assert _deliver_vrs(pending) == "failed"
    assert _vrs_claim_state(delivery_storage) == "sent"
    monkeypatch.setattr(main, "mark_content_processed", storage.mark_content_processed)
    assert _deliver_vrs(pending) == "reconciled"
    assert sent == ["album"]
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_vrs_busy_claim_preserves_unsent_outbox(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    uid = f"tournament-vrs-v1:global:{pending.match.vrs_baseline_id}"
    competing = asyncio.run(storage.claim_content_delivery(uid))
    assert _deliver_vrs(pending) == "duplicate"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1
    asyncio.run(storage.release_delivery_claim(competing))
    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: None)
    assert _deliver_vrs(pending) == "sent"


def test_vrs_confirmed_outbox_cleanup_recovers_without_resending(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery
    sent = []

    async def unavailable(*args):
        raise storage.StorageUnavailableError("simulated outbox cleanup failure")

    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: sent.append("album"))
    monkeypatch.setattr(main, "delete_result_delivery", unavailable)
    assert _deliver_vrs(pending) == "failed"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1
    monkeypatch.setattr(main, "delete_result_delivery", storage.delete_result_delivery)
    assert _deliver_vrs(pending) == "duplicate"
    assert sent == ["album"]
    assert asyncio.run(storage.list_pending_result_deliveries()) == []


def test_vrs_claim_failure_is_contained_and_retains_queue(monkeypatch, delivery_storage, vrs_delivery):
    pending, _ = vrs_delivery

    async def unavailable(*args):
        raise storage.StorageUnavailableError("simulated claim failure")

    monkeypatch.setattr(main, "claim_content_delivery", unavailable)
    assert _deliver_vrs(pending) == "failed"
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1


@pytest.mark.parametrize("job", ["schedule", "digest"])
@pytest.mark.parametrize("all_fail", [False, True])
def test_threads_render_failure_isolated_and_recovery_preserves_sent_posts(
    monkeypatch, delivery_storage, job, all_fail,
):
    first, second = (_upcoming(), _upcoming()) if job == "schedule" else (_match(), _match())
    first = first.model_copy(update={"match_id": "first", "tournament_name": "First event", "competition_key": "First event"})
    second = second.model_copy(update={"match_id": "second", "tournament_name": "Second event", "competition_key": "Second event"})
    matches = [first, second]
    local_now = datetime(2026, 10, 18, 10, tzinfo=timezone(timedelta(hours=3)))
    monkeypatch.setattr(main, "_local_day_window", lambda **kwargs: (local_now, local_now + timedelta(days=1), local_now))
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "@test"}])
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "fetch_upcoming_matches", lambda *args: _async(matches))
    monkeypatch.setattr(main, "_select_schedule_matches", lambda *args: matches)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", lambda *args: _async([None, None]))
    monkeypatch.setattr(main, "format_schedule_context_messages", lambda *args: [])
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *args, **kwargs: _async(matches))
    monkeypatch.setattr(main, "apply_quality_filters", lambda *args: (matches, [], []))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    recovered = False

    def render(members, *args, **kwargs):
        if len(members) == 1 and not recovered and (all_fail or members[0] is first):
            raise MediaCardError("simulated tournament renderer failure")
        return [b"card"]

    monkeypatch.setattr(main, "render_schedule_cards", render)
    monkeypatch.setattr(main, "render_results_cards", render)
    telegram, instagram, threads, notices = [], [], [], []
    monkeypatch.setattr(main, "send_photo_to_telegram", lambda *args, **kwargs: telegram.append(args[0]))
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *args: instagram.append(args[0]))
    monkeypatch.setattr(main, "_publish_threads_chain", lambda *args: threads.append(args[0]))
    monkeypatch.setattr(main, "_notify_admin", lambda *args: notices.append(args))

    body = json.loads(main._handle_content_job(job, False)["body"])
    assert body["messages_sent"] == body["instagram_messages_sent"] == 1
    assert body["threads_messages_sent"] == (0 if all_fail else 1)
    assert body["threads_delivery_failures"] == (2 if all_fail else 1)
    assert notices[0][0] == "threads_content_card_render_failed"

    recovered = True
    retried = json.loads(main._handle_content_job(job, False)["body"])
    assert retried["threads_messages_sent"] == (2 if all_fail else 1)
    assert retried["threads_delivery_failures"] == 0
    assert len(telegram) == len(instagram) == 1
    assert len(threads) == len(set(threads)) == 2
