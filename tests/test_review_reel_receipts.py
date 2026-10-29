"""A published Reel receipt uses its saved generation without another state GET."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from cs2bot import main, publication_admin, reel_delivery
from cs2bot.instagram_publish import InstagramDeliveryUncertainError
from cs2bot.match_sources import storage
from test_storage import FakeS3


NOW = datetime(2026, 10, 29, 6, 20, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()


@pytest.fixture
def receipt_runtime(monkeypatch):
    s3 = FakeS3()
    for module in (storage, publication_admin):
        monkeypatch.setattr(module, "_client", lambda: s3)
        monkeypatch.setattr(module, "_bucket", lambda: "offline")
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: True)
    monkeypatch.setattr(main, "_notify_admin", Mock())
    advance_today = reel_delivery.advance_today_reel
    monkeypatch.setattr(main, "advance_today_reel", lambda context, **kwargs:
                        advance_today(context, now=NOW, **kwargs))
    publish = Mock()
    monkeypatch.setattr(reel_delivery, "reel_container_status", lambda *args: "FINISHED")
    monkeypatch.setattr(reel_delivery, "publish_reel_container", publish)

    def no_network(*args, **kwargs):
        pytest.fail("receipt tests must not use real HTTP")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    return s3, publish


@pytest.mark.parametrize("control_enabled", [True, False])
def test_worker_receipt_keeps_saved_generation_when_owner_control_is_disabled(
    monkeypatch, receipt_runtime, control_enabled,
):
    s3, publish = receipt_runtime
    asyncio.run(publication_admin.update_policy(
        "instagram", "schedule_reel", False, actor_user_id=7, operation_id="off",
        expected_revision=0, client=s3, bucket="offline", now=NOW))
    asyncio.run(publication_admin.update_policy(
        "instagram", "schedule_reel", True, actor_user_id=7, operation_id="on",
        expected_revision=1, client=s3, bucket="offline", now=NOW))
    saved = reel_delivery.save_pending_reel(DAY, "12345", 4, generation=1)
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", control_enabled)
    assert main.publication_delivery_allowed("instagram", "schedule_reel") == (True, 1 if control_enabled else 0)
    load = Mock(wraps=reel_delivery.load_pending_reel)
    monkeypatch.setattr(reel_delivery, "load_pending_reel", load)

    assert main._retry_reel_worker(None) == {DAY: "published"}
    assert load.call_count == 1
    publish.assert_called_once_with(saved.container_id, None)
    records = asyncio.run(publication_admin.list_publications(client=s3, bucket="offline"))
    assert len(records) == 1
    assert records[0]["source_key"] == saved.content_uid
    assert records[0]["generation"] == records[0]["metadata"]["generation"] == 1


@pytest.mark.parametrize("outcome", ["processing", "held", "publish_uncertain", "reconciled"])
def test_nonpublished_reel_does_not_emit_a_new_receipt(monkeypatch, receipt_runtime, outcome):
    _, publish = receipt_runtime
    saved = reel_delivery.save_pending_reel(DAY, "12345", 4, generation=1)
    if outcome == "processing":
        monkeypatch.setattr(reel_delivery, "reel_container_status", lambda *args: "IN_PROGRESS")
    elif outcome == "publish_uncertain":
        publish.side_effect = InstagramDeliveryUncertainError("unknown")
    elif outcome == "reconciled":
        claim = asyncio.run(storage.claim_content_delivery(saved.content_uid))
        claim = asyncio.run(storage.mark_delivery_claim_attempting(claim))
        asyncio.run(storage.mark_delivery_claim_sent(claim))
    receipt = Mock()
    result = reel_delivery.advance_today_reel(
        None, now=NOW, current_generation=2 if outcome == "held" else 1, on_published=receipt,
    )
    assert result == {DAY: outcome}
    receipt.assert_not_called()


def test_existing_status_return_api_needs_no_receipt_callback(monkeypatch, receipt_runtime):
    reel_delivery.save_pending_reel(DAY, "12345", 4, generation=1)
    monkeypatch.setattr(reel_delivery, "reel_container_status", lambda *args: "IN_PROGRESS")
    assert reel_delivery.advance_today_reel(None, now=NOW, current_generation=1) == {DAY: "processing"}
