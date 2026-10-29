"""Regression coverage for held retry selection and cross-midnight final aliases."""
import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from cs2bot import main
from cs2bot.match_sources import storage
from cs2bot.match_sources.models import MatchNormalized
from cs2bot.publication_admin import PolicySnapshot


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None, **kwargs):
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body
        return {"ETag": '"memory"'}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": '"memory"'}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ETag": '"memory"'}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, **kwargs):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def match(**overrides):
    values = dict(source="pandascore", match_id="x", tournament_name="IEM Cologne 2026",
                  competition_key="IEM Cologne 2026", team1_name="NAVI", team2_name="FaZe",
                  score1=2, score2=1, best_of=3)
    values.update(overrides)
    return MatchNormalized(**values)


@pytest.fixture
def queue_runtime(monkeypatch):
    s3 = MemoryS3()
    monkeypatch.setattr(storage, "_client", lambda: s3)
    monkeypatch.setattr(storage, "_bucket", lambda: "bucket")
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    return s3


def seed_queue(s3, generations):
    async def seed():
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        for index, generation in enumerate(generations):
            await storage.enqueue_result_delivery(
                match(match_id=str(index)), "global", "global", client=s3, bucket="bucket",
                now=now + timedelta(seconds=index), generation=generation,
            )
    asyncio.run(seed())


def set_policy(monkeypatch, *, enabled=True, generation=1):
    calls = []

    async def read():
        calls.append("read")
        return PolicySnapshot(2, {"global:results": {"enabled": enabled, "generation": generation}}, None)

    monkeypatch.setattr(main, "read_policy", read)
    return calls


def test_retry_limit_is_applied_after_held_items_without_changing_queue(queue_runtime, monkeypatch):
    s3 = queue_runtime
    seed_queue(s3, [0] * 200 + [1])
    policy_reads = set_policy(monkeypatch)
    before = dict(s3.objects)
    first = asyncio.run(main._load_result_retry_targets())
    second = asyncio.run(main._load_result_retry_targets())
    assert [item.match.match_id for item in first] == ["200"]
    assert second == first
    assert policy_reads == ["read", "read"]  # One snapshot per selection.
    assert s3.objects == before  # Held generations and retry timestamps remain intact.
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 200
    assert len(asyncio.run(storage.list_pending_result_deliveries(limit=None))) == 201


@pytest.mark.parametrize("enabled,generations", [(False, [0, 1]), (True, [0, 0])])
def test_disabled_or_old_generations_stay_outside_retry_selection(
    queue_runtime, monkeypatch, enabled, generations,
):
    seed_queue(queue_runtime, generations)
    reads = set_policy(monkeypatch, enabled=enabled)
    assert asyncio.run(main._load_result_retry_targets()) == []
    assert reads == ["read"]
    assert len(queue_runtime.objects) == 2


def test_uninitialised_owner_policy_does_not_enable_retry(queue_runtime, monkeypatch):
    seed_queue(queue_runtime, [0])

    async def read():
        return PolicySnapshot(0, {}, None)

    monkeypatch.setattr(main, "read_policy", read)
    assert asyncio.run(main._load_result_retry_targets()) == []


def test_policy_read_failure_is_not_treated_as_empty_queue(queue_runtime, monkeypatch):
    seed_queue(queue_runtime, [1])

    async def read():
        raise storage.StorageUnavailableError("policy read unavailable")

    monkeypatch.setattr(main, "read_policy", read)
    with pytest.raises(storage.StorageUnavailableError, match="policy read unavailable"):
        asyncio.run(main._load_result_retry_targets())


def test_retry_policy_is_scoped_to_destination_and_content_type(queue_runtime, monkeypatch):
    async def seed():
        for destination, content_type, generation in [
            ("global", "result", 1), ("global", "tournament_standings", 2), ("threads", "result", 1),
        ]:
            await storage.enqueue_result_delivery(
                match(), destination, destination, client=queue_runtime, bucket="bucket",
                content_type=content_type, generation=generation,
            )

    asyncio.run(seed())

    async def read():
        return PolicySnapshot(3, {
            "global:results": {"enabled": False, "generation": 1},
            "global:tournament_standings": {"enabled": True, "generation": 2},
            "threads:results": {"enabled": True, "generation": 1},
        }, None)

    monkeypatch.setattr(main, "read_policy", read)
    pending = asyncio.run(main._load_result_retry_targets())
    assert {(item.channel_id, item.content_type) for item in pending} == {
        ("global", "tournament_standings"), ("threads", "result"),
    }


def test_control_off_preserves_retry_order_and_limit_without_policy_read(queue_runtime, monkeypatch):
    seed_queue(queue_runtime, [0] * 201)
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", False)

    async def read():
        pytest.fail("disabled control must not read owner policy")

    monkeypatch.setattr(main, "read_policy", read)
    pending = asyncio.run(main._load_result_retry_targets())
    assert [item.match.match_id for item in pending] == [str(index) for index in range(200)]


def test_empty_retry_does_not_read_owner_policy(queue_runtime, monkeypatch):
    async def read():
        pytest.fail("empty queue needs no owner policy read")

    monkeypatch.setattr(main, "read_policy", read)
    assert asyncio.run(main._load_result_retry_targets()) == []


def test_late_cross_midnight_primary_recognizes_existing_liquipedia_bridge():
    s3 = MemoryS3()
    primary = match(start_date="2026-10-28T23:30:00Z", date="2026-10-29T01:30:00Z",
                    end_date="2026-10-29T01:30:00Z")
    final = match(source="liquipedia", match_id="lp", start_date="2026-10-28T23:30:00Z",
                  date="2026-10-28T23:30:00Z", is_final=True, final_identity_confirmed=True)
    # This is the exact key written by the previous LP adapter/bridge algorithm.
    old_bridge = "final_bridge_v1_42763caff872124331d62a2c"
    s3.objects[storage.processed_key(f"global_{old_bridge}")] = b"{}"
    assert final.final_bridge_uid == old_bridge
    assert primary.final_bridge_uid == old_bridge
    # Canonical IDs and existing primary claims/outbox keys are deliberately unchanged.
    assert primary.match_uid == "match_v1_f1e5a3b9a8bc54c0361b9ae2"
    assert final.match_uid == "match_v1_42763caff872124331d62a2c"
    assert asyncio.run(storage.is_channel_processed(primary, "global", client=s3, bucket="bucket"))


@pytest.mark.parametrize("overrides", [
    {"tournament_name": "IEM Dallas 2026", "competition_key": "IEM Dallas 2026"},
    {"team1_name": "Vitality"},
    {"score1": 1, "score2": 2},
    {"start_date": "2026-10-29T23:30:00Z"},
])
def test_final_bridge_keeps_distinct_event_team_score_and_start_day(overrides):
    original = match(start_date="2026-10-28T23:30:00Z", end_date="2026-10-29T01:30:00Z")
    changed = original.model_copy(update=overrides)
    assert changed.final_bridge_uid != original.final_bridge_uid


def test_missing_start_preserves_bridge_end_date_precedence():
    primary = match(date="2026-10-28T23:30:00Z", end_date="2026-10-29T01:30:00Z")
    assert primary.final_bridge_uid == "final_bridge_v1_f1e5a3b9a8bc54c0361b9ae2"
