import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from botocore.exceptions import ClientError

from cs2bot.publication_admin import (POLICY_KEY, PolicySnapshot, list_publications,
                                      pending_delivery_status, policy_status, publication_allowed, publications_in_period,
                                      read_policy, record_publication,
                                      update_policy)


class FakeS3:
    def __init__(self): self.objects, self.version = {}, 0
    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None, **kwargs):
        current = self.objects.get(Key)
        if (IfNoneMatch == "*" and current) or (IfMatch and (not current or current["ETag"] != IfMatch)):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.version += 1; etag = f'"{self.version}"'
        self.objects[Key] = {"Body": Body, "ETag": etag}
        return {"ETag": etag}
    def get_object(self, Bucket, Key):
        if Key not in self.objects: raise ClientError({"Error": {"Code": "404"}}, "GetObject")
        item = self.objects[Key]
        return {"Body": io.BytesIO(item["Body"]), "ETag": item["ETag"]}
    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def test_policy_cas_and_queue_generation_hold_old_items():
    s3 = FakeS3()
    initial = asyncio.run(read_policy(client=s3, bucket="bucket"))
    assert asyncio.run(publication_allowed("global", "results", control_enabled=True, client=s3, bucket="bucket")) == (False, 0)
    off = asyncio.run(update_policy("global", "results", False, actor_user_id=7, operation_id="off", expected_revision=0, client=s3, bucket="bucket"))
    assert policy_status(off, "global", "results") == (False, 1)
    on = asyncio.run(update_policy("global", "results", True, actor_user_id=7, operation_id="on", expected_revision=1, client=s3, bucket="bucket"))
    assert policy_status(on, "global", "results") == (True, 1)
    assert POLICY_KEY in s3.objects
    try:
        asyncio.run(update_policy("global", "results", False, actor_user_id=7, operation_id="old", expected_revision=1, client=s3, bucket="bucket"))
    except ValueError as exc:
        assert str(exc) == "stale_policy_revision"
    else: raise AssertionError("stale callback changed policy")


def test_confirmed_journal_is_idempotent_and_paged_readable():
    s3 = FakeS3(); now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert asyncio.run(record_publication("telegram:global:schedule:x", "global", "telegram", "schedule", "x", client=s3, bucket="bucket", now=now))
    assert not asyncio.run(record_publication("telegram:global:schedule:x", "global", "telegram", "schedule", "x", client=s3, bucket="bucket", now=now))
    records = asyncio.run(list_publications(client=s3, bucket="bucket"))
    assert [(record["publication_type"], record["status"]) for record in records] == [("schedule", "sent")]


def test_periods_use_moscow_boundaries_and_old_generation_needs_manual_review():
    now = datetime(2026, 10, 4, 1, tzinfo=timezone.utc)  # 04:00 Moscow
    records = [
        {"confirmed_at": "2026-10-03T20:59:59Z", "publication_id": "old"},
        {"confirmed_at": "2026-10-03T21:00:00Z", "publication_id": "today"},
    ]
    assert [item["publication_id"] for item in publications_in_period(records, "today", now=now)] == ["today"]
    policy = PolicySnapshot(3, {"global:results": {"enabled": True, "generation": 1}}, '"3"')
    old = SimpleNamespace(channel_id="global", content_type="result", generation=0, attempt_count=0)
    fresh = SimpleNamespace(channel_id="global", content_type="result", generation=1, attempt_count=1)
    assert pending_delivery_status(old, policy) == "manual_review"
    assert pending_delivery_status(fresh, policy) == "retry_safe"
