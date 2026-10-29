"""Owner generations survive the durable schedule-Reel handoff."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
import requests

from cs2bot import main, publication_admin, reel_delivery
from cs2bot.match_sources import storage
from cs2bot.match_sources.models import UpcomingMatchNormalized
from test_storage import FakeS3


NOW = datetime(2026, 10, 29, 6, 15, tzinfo=timezone.utc)
DAY = "2026-10-29"


@pytest.fixture
def reel_runtime(monkeypatch):
    s3 = FakeS3()
    for module in (storage, publication_admin):
        monkeypatch.setattr(module, "_client", lambda: s3)
        monkeypatch.setattr(module, "_bucket", lambda: "bucket")
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: True)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: None)
    advance_today = reel_delivery.advance_today_reel
    monkeypatch.setattr(main, "advance_today_reel", lambda context, **kwargs:
                        advance_today(context, now=NOW, **kwargs))
    status, meta_reads, published = ["IN_PROGRESS"], [], []

    def read_status(container_id, context):
        meta_reads.append(container_id)
        return status[0]

    def forbid_network(*args, **kwargs):
        pytest.fail("generation tests must not use real HTTP")

    monkeypatch.setattr(requests.sessions.Session, "request", forbid_network)
    monkeypatch.setattr(reel_delivery, "reel_container_status", read_status)
    monkeypatch.setattr(reel_delivery, "publish_reel_container",
                        lambda container_id, context: published.append(container_id))
    return s3, status, meta_reads, published


def update_policy(enabled, revision):
    return asyncio.run(publication_admin.update_policy(
        "instagram", "schedule_reel", enabled, actor_user_id=7,
        operation_id=f"change-{revision}", expected_revision=revision, now=NOW,
    ))


def test_processing_reel_stays_held_after_owner_off_on(reel_runtime):
    _, status, meta_reads, published = reel_runtime
    update_policy(True, 0)
    saved = reel_delivery.save_pending_reel(DAY, "12345", 4, generation=0)
    assert main._retry_reel_worker(None) == {DAY: "processing"}
    update_policy(False, 1)
    status[0] = "FINISHED"
    assert main._retry_reel_worker(None) == {}
    update_policy(True, 2)
    assert main._retry_reel_worker(None) == {DAY: "held"}
    assert reel_delivery.load_pending_reel(DAY) == saved
    assert meta_reads == ["12345"] and published == []


def test_fresh_reel_in_current_generation_publishes(reel_runtime):
    _, status, _, published = reel_runtime
    update_policy(False, 0)
    update_policy(True, 1)
    saved = reel_delivery.save_pending_reel(DAY, "12345", 4, generation=1)
    assert reel_delivery.load_pending_reel(DAY).generation == saved.generation == 1
    status[0] = "FINISHED"
    assert main._retry_reel_worker(None) == {DAY: "published"}
    assert published == ["12345"]


def test_legacy_reel_without_generation_stays_held_after_toggle(reel_runtime):
    s3, status, meta_reads, published = reel_runtime
    update_policy(False, 0)
    update_policy(True, 1)
    legacy = reel_delivery.PendingReel(DAY, "12345", 4, NOW.isoformat()).__dict__.copy()
    legacy.pop("generation")
    s3.put_object(Bucket="bucket", Key=f"reels/{DAY}.json",
                  Body=json.dumps(legacy).encode(), ContentType="application/json")
    assert reel_delivery.load_pending_reel(DAY).generation == 0
    status[0] = "FINISHED"
    assert main._retry_reel_worker(None) == {DAY: "held"}
    assert meta_reads == [] and published == []


@pytest.mark.parametrize("terminal", ["sent", "uncertain"])
def test_current_generation_does_not_reopen_terminal_reel_claim(reel_runtime, terminal):
    _, status, meta_reads, published = reel_runtime
    update_policy(False, 0)
    update_policy(True, 1)
    saved = reel_delivery.save_pending_reel(DAY, "12345", 4, generation=1)
    claim = asyncio.run(storage.claim_content_delivery(saved.content_uid))
    claim = asyncio.run(storage.mark_delivery_claim_attempting(claim))
    transition = storage.mark_delivery_claim_sent if terminal == "sent" else storage.mark_delivery_claim_uncertain
    asyncio.run(transition(claim))
    status[0] = "FINISHED"
    expected = "reconciled" if terminal == "sent" else "duplicate_or_blocked"
    assert main._retry_reel_worker(None) == {DAY: expected}
    assert meta_reads == [] and published == []


def test_prepare_captures_generation_and_reuses_existing_policy_reads(reel_runtime, monkeypatch):
    _, status, _, published = reel_runtime
    update_policy(False, 0)
    update_policy(True, 1)
    reads = []
    read_policy = publication_admin.read_policy

    async def observe_read(**kwargs):
        reads.append("policy")
        return await read_policy(**kwargs)

    async def upcoming(*args):
        return [UpcomingMatchNormalized(
            match_id="match-1", tournament_name="IEM", tournament_tier="s",
            team1_name="NAVI", team2_name="FaZe", scheduled_at=NOW.isoformat(),
        )]

    monkeypatch.setattr(publication_admin, "read_policy", observe_read)
    monkeypatch.setattr(main, "_local_day_window", lambda: (NOW, NOW, NOW))
    monkeypatch.setattr(main, "fetch_upcoming_matches", upcoming)
    monkeypatch.setattr(main, "next_reel_audio_style", lambda day: "minimal")
    monkeypatch.setattr(main, "render_schedule_reel", lambda *args, **kwargs: b"offline-video")
    monkeypatch.setattr(main, "upload_public_reel", lambda *args: "https://example.invalid/reel.mp4")
    monkeypatch.setattr(main, "create_reel_container", lambda *args: "22222")
    status[0] = "FINISHED"
    result = main._handle_schedule_reel_job(False, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["state"] == "published"
    assert reel_delivery.load_pending_reel(DAY).generation == 1
    assert reads == ["policy", "policy"]  # No new policy/S3 read was added.
    assert published == ["22222"]


def test_prepare_keeps_origin_generation_if_owner_toggles_during_render(reel_runtime, monkeypatch):
    _, status, meta_reads, published = reel_runtime
    update_policy(True, 0)

    async def upcoming(*args):
        return [UpcomingMatchNormalized(
            match_id="match-1", tournament_name="IEM", tournament_tier="s",
            team1_name="NAVI", team2_name="FaZe", scheduled_at=NOW.isoformat(),
        )]

    def render(*args, **kwargs):
        update_policy(False, 1)
        update_policy(True, 2)
        return b"offline-video"

    monkeypatch.setattr(main, "_local_day_window", lambda: (NOW, NOW, NOW))
    monkeypatch.setattr(main, "fetch_upcoming_matches", upcoming)
    monkeypatch.setattr(main, "next_reel_audio_style", lambda day: "minimal")
    monkeypatch.setattr(main, "render_schedule_reel", render)
    monkeypatch.setattr(main, "upload_public_reel", lambda *args: "https://example.invalid/reel.mp4")
    monkeypatch.setattr(main, "create_reel_container", lambda *args: "22222")
    status[0] = "FINISHED"
    result = main._handle_schedule_reel_job(False, None)
    assert json.loads(result["body"])["state"] == "held"
    assert reel_delivery.load_pending_reel(DAY).generation == 0
    assert meta_reads == [] and published == []


@pytest.mark.parametrize("generation", [True, -1, "1", None])
def test_invalid_reel_generation_cannot_be_saved_or_loaded(reel_runtime, generation):
    s3, _, _, _ = reel_runtime
    with pytest.raises(ValueError, match="generation"):
        reel_delivery.save_pending_reel(DAY, "12345", 4, generation=generation)
    payload = reel_delivery.PendingReel(DAY, "12345", 4, NOW.isoformat()).__dict__.copy()
    payload["generation"] = generation
    s3.put_object(Bucket="bucket", Key=f"reels/{DAY}.json",
                  Body=json.dumps(payload).encode(), ContentType="application/json")
    with pytest.raises(storage.StorageUnavailableError, match="state is invalid"):
        reel_delivery.load_pending_reel(DAY)
