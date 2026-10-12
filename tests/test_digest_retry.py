import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from cs2bot import main
from cs2bot.digest_retry import DigestRetryQueue, LEASE, MAX_AGE, RETRY_DELAY
from cs2bot.match_sources.storage import DeliveryClaim, PendingDelivery, StorageUnavailableError
from test_main import configured_runtime, _async, _claim, _match
from test_storage import FakeS3

NOW = datetime(2026, 10, 8, 20, 59, tzinfo=timezone.utc)  # 23:59 MSK


def enqueue(queue, now=NOW, generations=None):
    start, end, display = main._local_day_window(now=now)
    return queue.enqueue_failure(start=start, end=end, display_at=display,
                                 generations=generations or {"global": 0, "instagram": 0, "threads": 0}, now=now)


@pytest.fixture
def queue():
    return DigestRetryQueue(FakeS3(), "bucket")


def test_three_attempts_are_bounded_and_preserve_original_day(queue):
    first = enqueue(queue)
    assert first.payload["attempts"] == 1
    assert queue.claim_due(NOW + RETRY_DELAY - timedelta(seconds=1)) is None
    second = queue.claim_due(NOW + RETRY_DELAY)
    assert second.day_key == "2026-10-08"
    assert second.payload["attempts"] == 2
    assert second.payload["window_start"] == "2026-10-07T21:00:00Z"
    assert second.payload["window_end"] == "2026-10-08T21:00:00Z"
    assert queue.claim_due(NOW + RETRY_DELAY) is None
    queue.finish(second, prepared=False, now=NOW + RETRY_DELAY)
    third = queue.claim_due(NOW + 2 * RETRY_DELAY)
    assert third.payload["attempts"] == 3
    closed = queue.finish(third, prepared=False, now=NOW + 2 * RETRY_DELAY)
    assert closed.payload["status"] == "exhausted"
    assert queue.claim_due(NOW + 3 * RETRY_DELAY) is None


def test_duplicate_enqueue_cannot_replace_queue_or_generations(queue):
    original = enqueue(queue)
    assert enqueue(queue, generations={"global": 9}) is None
    assert queue._load(original.day_key).payload == original.payload


def test_prepared_and_empty_editions_do_not_retry(queue):
    enqueue(queue)
    attempt = queue.claim_due(NOW + RETRY_DELAY)
    queue.finish(attempt, prepared=True, now=NOW + RETRY_DELAY)
    assert queue.claim_due(NOW + 2 * RETRY_DELAY) is None


def test_expiry_closes_without_external_attempt(queue):
    enqueue(queue)
    expired = queue.claim_due(NOW + MAX_AGE)
    assert expired.payload["status"] == "exhausted"
    assert expired.payload["attempts"] == 1
    assert queue.claim_due(NOW + MAX_AGE + RETRY_DELAY) is None


def test_crash_lease_and_stale_worker_are_safe(queue):
    enqueue(queue)
    old = queue.claim_due(NOW + RETRY_DELAY)
    assert queue.claim_due(NOW + RETRY_DELAY + LEASE - timedelta(seconds=1)) is None
    current = queue.claim_due(NOW + RETRY_DELAY + LEASE)
    assert current.payload["attempts"] == 3
    assert queue.finish(old, prepared=True) is None
    # A crashed final attempt is exhausted after its lease, not retried for a fourth time.
    expired = queue.claim_due(NOW + RETRY_DELAY + 2 * LEASE)
    assert expired.payload["status"] == "exhausted"


def test_concurrent_workers_have_one_lease():
    class ConcurrentS3(FakeS3):
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def put_object(self, **kwargs):
            if kwargs.get("IfMatch"):
                self.barrier.wait(timeout=3)
            with self.lock:
                return super().put_object(**kwargs)

    from concurrent.futures import ThreadPoolExecutor
    queue = DigestRetryQueue(ConcurrentS3(), "bucket")
    enqueue(queue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: queue.claim_due(NOW + RETRY_DELAY), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("corruption", [{"schema_version": 2}, {"attempts": True},
                                        {"generations": {"global": -1}}, {"window_end": "wrong"}])
def test_corrupt_state_fails_closed(queue, corruption):
    task = enqueue(queue)
    item = queue.client.objects[task.key]
    item["Body"] = json.dumps({**task.payload, **corruption}).encode()
    with pytest.raises(StorageUnavailableError):
        queue.claim_due(NOW + RETRY_DELAY)


def test_storage_failure_does_not_claim_or_publish():
    class BrokenS3(FakeS3):
        def get_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    with pytest.raises(StorageUnavailableError):
        DigestRetryQueue(BrokenS3(), "bucket").claim_due(NOW)


@pytest.fixture
def runtime(monkeypatch, configured_runtime):
    clock = [NOW]
    s3 = FakeS3()

    class ClockQueue(DigestRetryQueue):
        def __init__(self):
            super().__init__(s3, "bucket")

        def enqueue_failure(self, **kwargs):
            return super().enqueue_failure(**kwargs, now=clock[0])

        def claim_due(self):
            return super().claim_due(now=clock[0])

        def finish(self, attempt, **kwargs):
            return super().finish(attempt, **kwargs, now=clock[0])

    original_window = main._local_day_window
    monkeypatch.setattr(main, "_local_day_window", lambda **kw: original_window(now=clock[0], **kw))
    monkeypatch.setattr(main, "DigestRetryQueue", ClockQueue)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "Global", "chat_id": "fake-chat"}])
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", False)
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async(DeliveryClaim(uid, f"claims/{uid}", uid)))
    monkeypatch.setattr(main, "mark_content_processed", lambda *a: _async(None))
    monkeypatch.setattr(main, "release_delivery_claim", lambda *a: _async(None))
    monkeypatch.setattr(main, "_record_post_analytics", lambda *a, **k: None)
    monkeypatch.setattr(main, "_enqueue_tournament_standings", lambda *a: False)
    alerts, sends, windows = [], [], []
    monkeypatch.setattr(main, "_notify_admin", lambda *a: alerts.append(a))
    monkeypatch.setattr(main, "send_to_telegram", lambda *a, **k: sends.append(a))
    return clock, s3, ClockQueue(), alerts, sends, windows


def failure(*args, **kwargs):
    raise main.StorageUnavailableError("PandaScore request failed: TimeoutError")


def initial_failure(monkeypatch):
    async def fail(*args, **kwargs):
        failure()
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", fail)
    return main._handle_content_job("digest", False)


def test_initial_error_is_queued_and_dry_run_has_no_effect(monkeypatch, runtime):
    _, s3, _, alerts, sends, _ = runtime
    assert initial_failure(monkeypatch)["statusCode"] == 502
    assert alerts == sends == []
    assert len(s3.objects) == 1
    s3.objects.clear()
    assert main._handle_content_job("digest", True)["statusCode"] == 502
    assert s3.objects == {} and alerts == []


def test_retry_worker_recovers_after_midnight_with_old_date(monkeypatch, runtime):
    clock, _, queue, alerts, sends, windows = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY

    async def fetch(limit, *, start, end):
        windows.append((start, end))
        return [_match()]

    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", fetch)
    response = main.handler({"retry_only": True}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["metrics"]["digest_retry_state"] == "prepared"
    assert windows[0] == (datetime(2026, 10, 7, 21, tzinfo=timezone.utc), datetime(2026, 10, 8, 21, tzinfo=timezone.utc))
    assert len(sends) == 1 and "8 октября" in sends[0][1]
    assert queue._load("2026-10-08").payload["status"] == "prepared"
    assert alerts == []
    clock[0] += RETRY_DELAY
    main.handler({"retry_only": True}, None)
    assert len(sends) == 1


def test_third_preparation_failure_sends_one_safe_alert(monkeypatch, runtime):
    clock, _, queue, alerts, sends, _ = runtime
    initial_failure(monkeypatch)
    for _ in range(2):
        clock[0] += RETRY_DELAY
        main.handler({"retry_only": True}, None)
    assert len(alerts) == 1
    assert "после 3 попыток" in alerts[0][1] and "2026-10-08" in alerts[0][1]
    assert "PandaScore" in alerts[0][1] and "не ответил" in alerts[0][1]
    assert sends == [] and queue._load("2026-10-08").payload["status"] == "exhausted"
    clock[0] += RETRY_DELAY
    main.handler({"retry_only": True}, None)
    assert len(alerts) == 1


@pytest.mark.parametrize("generation", [1, 2])
def test_old_digest_does_not_resume_after_owner_toggle(monkeypatch, runtime, generation):
    clock, _, queue, alerts, sends, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, generation))
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([_match()]))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "render_results_cards", lambda *a: [b"card"])
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: pytest.fail("old Instagram edition"))
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *a: pytest.fail("old Threads edition"))
    response = main._handle_content_job("digest", False, digest_attempt=queue.claim_due())
    assert response["statusCode"] == 200 and sends == [] and alerts == []
    # New editions capture the current generation, including generations above zero.
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    main._handle_content_job("digest", False)
    assert len(sends) == 1


def test_stale_preparation_worker_cannot_send(monkeypatch, runtime):
    clock, _, queue, _, sends, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    stale = queue.claim_due()
    clock[0] += LEASE
    queue.claim_due()
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([_match()]))
    assert main._handle_content_job("digest", False, digest_attempt=stale)["statusCode"] == 409
    assert sends == []


def test_recovered_digest_uses_stable_ids_on_all_platforms(monkeypatch, runtime):
    clock, _, queue, alerts, sends, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    ids, meta = [], []
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([_match()]))
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async(ids.append(uid) or DeliveryClaim(uid, uid, uid)))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "render_results_cards", lambda *a: [b"card"])
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: meta.append(a) or "instagram-id")
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *a: meta.append(a) or "threads-id")
    body = json.loads(main._handle_content_job("digest", False, digest_attempt=queue.claim_due())["body"])
    assert body["messages_sent"] == body["instagram_messages_sent"] == body["threads_messages_sent"] == 1
    assert len(sends) == 1 and len(meta) == 2 and alerts == []
    assert len(ids) == 3 and all("2026-10-08" in uid for uid in ids)


@pytest.mark.parametrize("destination", ["global", "instagram", "threads"])
def test_daily_owner_change_before_attempting_blocks_request(monkeypatch, runtime, destination):
    _, _, _, _, sends, _ = runtime
    generation = [0]
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, generation[0]))
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([_match()]))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: destination == "instagram")
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: destination == "threads")
    monkeypatch.setattr(main, "render_results_cards", lambda *a: [b"card"])

    def claim(uid):
        if (uid.startswith("digest_") and destination == "global") or uid.startswith(destination + "_"):
            generation[0] = 1
        return _async(DeliveryClaim(uid, uid, uid))

    monkeypatch.setattr(main, "claim_content_delivery", claim)
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: pytest.fail("Instagram policy race"))
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *a: pytest.fail("Threads policy race"))
    main._handle_content_job("digest", False)
    assert len(sends) == (0 if destination == "global" else 1)


def test_preparation_write_failure_blocks_external_delivery(monkeypatch, runtime):
    clock, _, queue, alerts, sends, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    attempt = queue.claim_due()
    monkeypatch.setattr(queue.client, "put_object", failure)
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([_match()]))
    response = main._handle_content_job("digest", False, digest_attempt=attempt)
    assert response["statusCode"] == 503 and sends == []
    assert len(alerts) == 1 and alerts[0][0] == "digest_retry_state_failed"


def test_empty_digest_completes_without_alert_or_send(monkeypatch, runtime):
    clock, _, queue, alerts, sends, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", lambda *a, **k: _async([]))
    response = main._handle_content_job("digest", False, digest_attempt=queue.claim_due())
    assert response["statusCode"] == 200 and sends == alerts == []
    assert queue._load("2026-10-08").payload["status"] == "prepared"


def test_busy_results_worker_defers_digest_before_claim(monkeypatch, runtime):
    clock, _, queue, alerts, _, _ = runtime
    initial_failure(monkeypatch)
    clock[0] += RETRY_DELAY
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(main, "_monotonic", lambda: next(ticks, 50.0))
    main.handler({"retry_only": True}, None)
    assert queue._load("2026-10-08").payload["attempts"] == 1 and alerts == []


def setup_instagram(monkeypatch, attempts=0):
    match = _match()
    pending = PendingDelivery("outbox/results/instagram.json", "instagram", "Instagram", match,
                              "2026-10-08T20:00:00Z", attempt_count=attempts)
    alerts, sent = [], []
    monkeypatch.setattr(main, "_notify_admin", lambda *a: alerts.append(a))
    monkeypatch.setattr(main, "claim_channel_delivery", lambda *a: _async(_claim(match, "instagram")))
    monkeypatch.setattr(main, "release_delivery_claim", lambda *a: _async(None))
    monkeypatch.setattr(main, "render_result_card", lambda *a: b"card")
    monkeypatch.setattr(main, "_enqueue_tournament_standings", lambda *a: False)
    monkeypatch.setattr(main, "mark_channel_processed", lambda *a: _async(None))
    monkeypatch.setattr(main, "_record_post_analytics", lambda *a, **k: None)
    monkeypatch.setattr(main, "record_result_delivery_attempt", lambda p: _async(replace(p, attempt_count=p.attempt_count + 1)))

    def reject(*args):
        sent.append(args)
        raise main.InstagramPublishError("HTTP 400")

    monkeypatch.setattr(main, "publish_rendered_cards", reject)
    return pending, alerts, sent


@pytest.mark.parametrize("previous,expected_alerts", [(0, 0), (1, 0), (2, 1), (3, 1)])
def test_instagram_safe_failures_alert_after_three(monkeypatch, configured_runtime, previous, expected_alerts):
    pending, alerts, sent = setup_instagram(monkeypatch, previous)
    assert main._deliver_instagram_result(pending, None) == "failed"
    assert len(alerts) == expected_alerts and len(sent) == 1


def test_instagram_uncertain_still_alerts_immediately_without_retry(monkeypatch, configured_runtime):
    pending, alerts, _ = setup_instagram(monkeypatch)
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: (_ for _ in ()).throw(main.InstagramDeliveryUncertainError("unknown")))
    monkeypatch.setattr(main, "record_result_delivery_attempt", lambda *a: pytest.fail("uncertain retry"))
    monkeypatch.setattr(main, "release_delivery_claim", lambda *a: pytest.fail("uncertain release"))
    assert main._deliver_instagram_result(pending, None) == "uncertain"
    assert len(alerts) == 1 and alerts[0][0] == "instagram_delivery_uncertain"


def test_instagram_confirmed_delivery_state_failure_is_not_delayed(monkeypatch, configured_runtime):
    pending, alerts, _ = setup_instagram(monkeypatch)
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: "external-id")
    monkeypatch.setattr(main, "mark_channel_processed", lambda *a: failure())
    monkeypatch.setattr(main, "release_delivery_claim", lambda *a: pytest.fail("already sent"))
    assert main._deliver_instagram_result(pending, None) == "failed"
    assert len(alerts) == 1 and alerts[0][0] == "instagram_confirmation_state_failed"


def test_instagram_retry_state_failure_is_not_hidden(monkeypatch, configured_runtime):
    pending, alerts, _ = setup_instagram(monkeypatch)
    monkeypatch.setattr(main, "record_result_delivery_attempt", failure)
    assert main._deliver_instagram_result(pending, None) == "failed"
    assert len(alerts) == 1 and alerts[0][0] == "instagram_retry_state_failed"


def test_instagram_release_failure_is_not_hidden(monkeypatch, configured_runtime):
    pending, alerts, _ = setup_instagram(monkeypatch)
    monkeypatch.setattr(main, "release_delivery_claim", failure)
    assert main._deliver_instagram_result(pending, None) == "failed"
    assert len(alerts) == 1 and alerts[0][0] == "instagram_retry_state_failed"


def test_instagram_success_after_transient_failure_has_no_alert(monkeypatch, configured_runtime):
    pending, alerts, _ = setup_instagram(monkeypatch, 1)
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *a: "instagram-id")
    assert main._deliver_instagram_result(pending, None) == "sent"
    assert alerts == []


@pytest.mark.parametrize("previous,expected_alerts", [(0, 0), (2, 1)])
def test_handler_does_not_add_second_instagram_alert(monkeypatch, configured_runtime, previous, expected_alerts):
    pending, alerts, _ = setup_instagram(monkeypatch, previous)
    monkeypatch.setattr(main, "get_new_finished_matches", lambda **k: _async([]))
    monkeypatch.setattr(main, "list_pending_result_deliveries", lambda **k: _async([pending]))
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "Global", "chat_id": "fake-chat"}])
    body = json.loads(main.handler({"retry_only": True}, None)["body"])
    assert body["delivery_failures"] == 1
    assert len(alerts) == expected_alerts
