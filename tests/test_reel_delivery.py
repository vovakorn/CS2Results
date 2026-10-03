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


class HistoryClient:
    def __init__(self, states, pages=None):
        self.states = {state.day_key: state for state in states}
        self.pages = pages or [{"Contents": [{"Key": f"reels/{day}.json"} for day in self.states]}]
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]

    def get_object(self, **kwargs):
        state = self.states[kwargs["Key"][6:-5]]
        return {"Body": io.BytesIO(json.dumps(state.__dict__).encode())}


@pytest.mark.parametrize("previous,expected", [(None, "esports"), ("esports", "minimal"), ("minimal", "esports")])
def test_audio_alternates_persisted_editions_not_calendar_days(previous, expected):
    state = reel_delivery.PendingReel("2026-09-25", "12345", 4, STATE.created_at, previous)
    assert reel_delivery.next_reel_audio_style("2026-10-03", client=HistoryClient([state]), bucket="state") == expected


def test_audio_first_edition_and_pagination_ignore_current_future_and_non_states():
    assert reel_delivery.next_reel_audio_style("2026-10-03", client=HistoryClient([]), bucket="state") == "esports"
    old = reel_delivery.PendingReel("2026-09-24", "12345", 4, STATE.created_at, "minimal")
    latest = reel_delivery.PendingReel("2026-09-25", "12345", 4, STATE.created_at, "esports")
    pages = [
        {"Contents": [{"Key": "reels/2026-09-24.json"}], "IsTruncated": True, "NextContinuationToken": "next"},
        {"Contents": [{"Key": key} for key in ["reels/2026-09-25.json", "reels/2026-10-03.json",
          "reels/2026-10-04.json", "reels/invalid.json", "reels/20260930.json", "reels/2026-10-01.mp4"]]},
    ]
    client = HistoryClient([old, latest], pages)
    assert reel_delivery.next_reel_audio_style("2026-10-03", client=client, bucket="state") == "minimal"
    assert client.calls[1]["ContinuationToken"] == "next"


def test_audio_history_does_not_silently_reset_on_storage_failure():
    class Client:
        def list_objects_v2(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
    with pytest.raises(reel_delivery.storage.StorageUnavailableError):
        reel_delivery.next_reel_audio_style("2026-10-03", client=Client(), bucket="state")
    with pytest.raises(reel_delivery.storage.StorageUnavailableError):
        reel_delivery.next_reel_audio_style("2026-10-03", client=HistoryClient([], [{"IsTruncated": True}]), bucket="state")


def test_selected_audio_is_saved_and_first_container_keeps_its_audio():
    saved = []
    class Client:
        def put_object(self, **kwargs):
            saved.append(json.loads(kwargs["Body"]))
    state = reel_delivery.save_pending_reel("2026-10-03", "99999", 4,
        audio_style="minimal", client=Client(), bucket="state")
    assert state.audio_style == saved[0]["audio_style"] == "minimal"
    class ConflictClient:
        def put_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(state.__dict__).encode())}
    assert reel_delivery.save_pending_reel("2026-10-03", "77777", 4,
        audio_style="esports", client=ConflictClient(), bucket="state") == state


@pytest.mark.parametrize("audio_style", [None, "esports", "minimal", "original", "melodic"])
def test_legacy_state_read_and_only_two_new_sounds(audio_style):
    payload = dict(STATE.__dict__)
    if audio_style is None:
        payload.pop("audio_style")
    else:
        payload["audio_style"] = audio_style
    class Client:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(payload).encode())}
    if audio_style in (None, "esports", "minimal"):
        assert reel_delivery.load_pending_reel(STATE.day_key, client=Client(), bucket="state").audio_style == audio_style
    else:
        with pytest.raises(reel_delivery.storage.StorageUnavailableError):
            reel_delivery.load_pending_reel(STATE.day_key, client=Client(), bucket="state")
        with pytest.raises(ValueError):
            reel_delivery.save_pending_reel(STATE.day_key, "12345", 4, audio_style=audio_style, client=Client(), bucket="state")


def test_worker_only_loads_current_day(monkeypatch):
    looked_up = []
    monkeypatch.setattr(reel_delivery, "load_pending_reel", lambda day: looked_up.append(day) or None)

    assert reel_delivery.advance_today_reel(None, datetime(2026, 9, 26, 0, 5, tzinfo=ZoneInfo("Europe/Moscow"))) == {}
    assert looked_up == ["2026-09-26"]
