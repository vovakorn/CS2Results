import io
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

from cs2bot import reel_delivery
from cs2bot.instagram_publish import InstagramDeliveryUncertainError


STATE = reel_delivery.PendingReel("2026-09-25", "12345", 4, "2026-09-25T06:15:00Z")


@dataclass
class Claim:
    state: str = "sending"


def setup_claims(monkeypatch, status):
    calls = []
    async def reconcile(*args, **kwargs):
        return False
    async def claim(*args, **kwargs):
        calls.append("claim")
        return Claim()
    async def release(*args, **kwargs):
        calls.append("release")
    async def attempting(*args, **kwargs):
        calls.append("attempting")
        return Claim("attempting")
    async def uncertain(*args, **kwargs):
        calls.append("uncertain")
        return Claim("uncertain")
    async def sent(*args, **kwargs):
        calls.append("sent")
        return Claim("sent")
    async def processed(*args, **kwargs):
        calls.append("processed")
    monkeypatch.setattr(reel_delivery.storage, "reconcile_content_delivery", reconcile)
    monkeypatch.setattr(reel_delivery.storage, "claim_content_delivery", claim)
    monkeypatch.setattr(reel_delivery.storage, "release_delivery_claim", release)
    monkeypatch.setattr(reel_delivery.storage, "mark_delivery_claim_attempting", attempting)
    monkeypatch.setattr(reel_delivery.storage, "mark_delivery_claim_uncertain", uncertain)
    monkeypatch.setattr(reel_delivery.storage, "mark_delivery_claim_sent", sent)
    monkeypatch.setattr(reel_delivery.storage, "mark_content_processed", processed)
    monkeypatch.setattr(reel_delivery, "reel_container_status", lambda *args: status)
    return calls


def test_processing_container_is_not_published(monkeypatch):
    calls = setup_claims(monkeypatch, "IN_PROGRESS")
    monkeypatch.setattr(reel_delivery, "publish_reel_container", lambda *args: pytest.fail("published too early"))

    assert reel_delivery.advance_pending_reel(STATE, None) == "processing"
    assert calls == ["claim", "release"]


def test_finished_container_is_published_once_after_attempting(monkeypatch):
    calls = setup_claims(monkeypatch, "FINISHED")
    monkeypatch.setattr(reel_delivery, "publish_reel_container", lambda *args: calls.append("publish"))

    assert reel_delivery.advance_pending_reel(STATE, None) == "published"
    assert calls == ["claim", "attempting", "publish", "sent", "processed"]


def test_uncertain_publish_retains_nonreclaimable_claim(monkeypatch):
    calls = setup_claims(monkeypatch, "FINISHED")
    def uncertain(*args):
        raise InstagramDeliveryUncertainError("unknown")
    monkeypatch.setattr(reel_delivery, "publish_reel_container", uncertain)

    assert reel_delivery.advance_pending_reel(STATE, None) == "publish_uncertain"
    assert calls == ["claim", "attempting", "uncertain"]


def test_failed_processing_blocks_publication(monkeypatch):
    calls = setup_claims(monkeypatch, "ERROR")
    monkeypatch.setattr(reel_delivery, "publish_reel_container", lambda *args: pytest.fail("published failed container"))

    assert reel_delivery.advance_pending_reel(STATE, None) == "processing_failed"
    assert calls == ["claim", "uncertain"]


def test_state_first_writer_wins():
    class Client:
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            raise ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(STATE.__dict__).encode())}

    assert reel_delivery.save_pending_reel("2026-09-25", "99999", 4, client=Client(), bucket="state") == STATE


def test_worker_only_loads_current_day(monkeypatch):
    looked_up = []
    monkeypatch.setattr(reel_delivery, "load_pending_reel", lambda day: looked_up.append(day) or None)

    assert reel_delivery.advance_today_reel(None, datetime(2026, 9, 26, 0, 5, tzinfo=ZoneInfo("Europe/Moscow"))) == {}
    assert looked_up == ["2026-09-26"]
