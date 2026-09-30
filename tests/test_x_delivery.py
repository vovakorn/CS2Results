import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
import pytest
import requests

from cs2bot import buffer_x_publish
from cs2bot import x_delivery
from cs2bot.match_sources.storage import (
    StorageUnavailableError,
    claim_x_delivery_create,
    get_x_delivery,
    prepare_x_delivery,
    x_delivery_key,
)
from cs2bot.x_delivery import check_due_x_deliveries, create_x_delivery


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.version = 0

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None, IfNoneMatch=None, IfMatch=None):
        previous = self.objects.get(Key)
        if IfNoneMatch == "*" and previous is not None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        if IfMatch and (previous is None or previous["ETag"] != IfMatch):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.version += 1
        etag = f'"etag-{self.version}"'
        self.objects[Key] = {"Body": Body, "ETag": etag, "Metadata": Metadata or {}}
        return {"ETag": etag}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        item = self.objects[Key]
        return {"Body": io.BytesIO(item["Body"]), "ETag": item["ETag"]}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


class BarrierXReadS3(FakeS3):
    """Force competing invocations to read the same ETag before their CAS writes."""

    def __init__(self):
        super().__init__()
        self.read_barrier = threading.Barrier(2)
        self.write_lock = threading.Lock()
        self.barrier_reads_remaining = 4

    def get_object(self, Bucket, Key):
        result = super().get_object(Bucket, Key)
        if Key.startswith("x/deliveries/") and self.barrier_reads_remaining:
            with self.write_lock:
                self.barrier_reads_remaining -= 1
            self.read_barrier.wait(timeout=5)
        return result

    def put_object(self, *args, **kwargs):
        with self.write_lock:
            return super().put_object(*args, **kwargs)


class FailOnceOnStatusS3(FakeS3):
    def __init__(self, status):
        super().__init__()
        self.fail_status = status
        self.failed = False

    def put_object(self, *args, **kwargs):
        body = kwargs.get("Body", b"")
        payload = json.loads(body) if isinstance(body, (bytes, bytearray)) else {}
        if not self.failed and payload.get("status") == self.fail_status:
            self.failed = True
            raise ClientError(
                {"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}},
                "PutObject",
            )
        return super().put_object(*args, **kwargs)


class FakeBuffer:
    def __init__(self, *, created=None, posts=None, recent=None, create_error=None, read_error=None):
        self.created = created or {"id": "buffer-1"}
        self.posts = posts or {}
        self.recent = recent or []
        self.create_error = create_error
        self.read_error = read_error
        self.create_calls = []
        self.get_calls = []
        self.search_calls = []

    def create_x_post(self, api_key, channel_id, text, image_urls, *, http_client):
        self.create_calls.append((api_key, channel_id, text, list(image_urls)))
        if self.create_error:
            raise self.create_error
        return self.created

    def get_post(self, api_key, post_id, *, http_client):
        self.get_calls.append(post_id)
        if self.read_error:
            raise self.read_error
        return self.posts[post_id]

    def find_recent_channel_posts(
        self, api_key, organization_id, channel_id, created_after, *, created_before, first, max_pages, http_client
    ):
        self.search_calls.append((organization_id, channel_id, created_after, created_before, first, max_pages))
        if self.read_error:
            raise self.read_error
        return self.recent


NOW = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
IMAGE_URLS = ("https://cdn.example/x/pub/1.png", "https://cdn.example/x/pub/2.png")
TEXT = "CS2 · 2026-09-29\nNAVI 2:1 FaZe"


def _post(status="scheduled", external_link=None, post_id="buffer-1"):
    return {
        "id": post_id,
        "channelId": "channel-x",
        "text": TEXT,
        "status": status,
        "createdAt": "2026-09-29T18:00:01Z",
        "externalLink": external_link,
        "assets": [{"source": url} for url in IMAGE_URLS],
    }


def _create(s3, buffer, uid="x:result:one", now=NOW):
    return asyncio.run(
        create_x_delivery(
            uid,
            "channel-x",
            "org-1",
            TEXT,
            IMAGE_URLS,
            "test-key",
            client=s3,
            bucket="bucket",
            buffer_api=buffer,
            now=now,
        )
    )


@pytest.mark.parametrize("status", ["scheduled", "sending"])
def test_delayed_buffer_post_remains_accepted_and_is_checked_only_when_due(status):
    s3 = FakeS3()
    buffer = FakeBuffer(posts={"buffer-1": _post(status)})
    accepted = _create(s3, buffer)

    early = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=4),
        )
    )
    assert early == []
    assert buffer.get_calls == []

    due = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=5),
        )
    )
    assert accepted.record.status == "accepted"
    assert due[0].record.status == "accepted"
    assert due[0].record.next_check_at == "2026-09-29T18:10:00Z"
    assert buffer.get_calls == ["buffer-1"]
    assert buffer.create_calls and len(buffer.create_calls) == 1


def test_concurrent_create_invocations_call_buffer_create_only_once():
    s3 = BarrierXReadS3()
    buffer = FakeBuffer()
    asyncio.run(
        prepare_x_delivery(
            "x:concurrent", TEXT, IMAGE_URLS,
            client=s3, bucket="bucket", now=NOW, channel_id="channel-x",
        )
    )

    def invoke():
        return _create(s3, buffer, uid="x:concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))

    assert {result.record.status for result in results} <= {"attempting", "accepted"}
    assert len(buffer.create_calls) == 1
    persisted = asyncio.run(
        get_x_delivery("x:concurrent", client=s3, bucket="bucket", channel_id="channel-x")
    )
    assert persisted is not None and persisted.record.status == "accepted"


def test_buffer_sent_with_external_link_marks_delivery_sent():
    s3 = FakeS3()
    link = "https://x.com/cs2results/status/123"
    buffer = FakeBuffer(posts={"buffer-1": _post("sent", link)})
    _create(s3, buffer)

    due = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=5),
        )
    )
    assert due[0].record.status == "sent"
    assert due[0].record.x_url == link
    assert not any(key.startswith(("claims/", "processed/")) for key in s3.objects)


def test_buffer_sent_without_link_stays_accepted_and_emits_diagnostic():
    s3 = FakeS3()
    buffer = FakeBuffer(posts={"buffer-1": _post("sent", None)})
    _create(s3, buffer)

    due = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=5),
        )
    )
    assert due[0].record.status == "accepted"
    assert due[0].record.x_url is None
    events = [json.loads(item["Body"]) for key, item in s3.objects.items() if key.startswith("analytics/x_delivery_recovery/")]
    assert [event["event"] for event in events] == ["sent_post_missing_x_link"]
    again = _create(s3, buffer)
    assert again.record.status == "accepted"
    assert len(buffer.create_calls) == 1


def test_buffer_status_error_reschedules_accepted_without_create_retry():
    s3 = FakeS3()
    buffer = FakeBuffer(read_error=requests.Timeout("temporary status timeout"))
    accepted = _create(s3, buffer)

    due = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=5),
        )
    )
    assert accepted.record.status == "accepted"
    assert due[0].record.status == "accepted"
    assert buffer.create_calls and len(buffer.create_calls) == 1
    events = [json.loads(item["Body"]) for key, item in s3.objects.items() if key.startswith("analytics/x_delivery_recovery/")]
    assert "buffer_status_check_failed" in [event["event"] for event in events]


def test_timeout_after_actual_buffer_create_recovers_exact_post_without_retry():
    s3 = FakeS3()
    buffer = FakeBuffer(
        recent=[_post("scheduled")],
        create_error=buffer_x_publish.BufferXDeliveryUncertainError("read timed out"),
    )
    accepted = _create(s3, buffer, uid="x:timeout:after-create")
    assert accepted.record.status == "accepted"
    assert accepted.record.buffer_post_id == "buffer-1"
    assert len(buffer.create_calls) == 1
    assert len(buffer.search_calls) == 1
    assert buffer.search_calls[0][1:] == (
        "channel-x",
        NOW - timedelta(seconds=30),
        NOW + timedelta(minutes=5),
        50,
        1,
    )

    again = _create(s3, buffer, uid="x:timeout:after-create", now=NOW + timedelta(minutes=1))
    assert again.record.status == "accepted"
    assert len(buffer.create_calls) == 1


def test_ambiguous_recent_search_marks_uncertain_and_records_event():
    s3 = FakeS3()
    buffer = FakeBuffer(
        recent=[_post("scheduled", post_id="buffer-a"), _post("scheduled", post_id="buffer-b")],
        create_error=buffer_x_publish.BufferXDeliveryUncertainError("connection dropped"),
    )
    result = _create(s3, buffer, uid="x:ambiguous")
    assert result.record.status == "uncertain"
    assert len(buffer.create_calls) == 1
    assert len(buffer.search_calls) == 1
    events = [json.loads(item["Body"]) for key, item in s3.objects.items() if key.startswith("analytics/x_delivery_recovery/")]
    assert "recent_post_match_not_unique" in [event["event"] for event in events]

    again = _create(s3, buffer, uid="x:ambiguous", now=NOW + timedelta(minutes=1))
    assert again.record.status == "uncertain"
    assert len(buffer.create_calls) == 1


def test_recovery_without_match_is_uncertain_and_never_creates_again():
    s3 = FakeS3()
    buffer = FakeBuffer(
        recent=[],
        create_error=buffer_x_publish.BufferXDeliveryUncertainError("timed out"),
    )
    result = _create(s3, buffer, uid="x:no-match")
    assert result.record.status == "uncertain"
    assert len(buffer.create_calls) == 1


@pytest.mark.parametrize(
    "candidate",
    [
        {**_post(), "channelId": "another-channel"},
        {**_post(), "createdAt": "2026-09-29T18:05:01Z"},
        {**_post(), "text": f"{TEXT}!"},
        {**_post(), "assets": [{"source": IMAGE_URLS[0]}]},
    ],
)
def test_recovery_requires_channel_time_exact_text_and_all_card_urls(candidate):
    s3 = FakeS3()
    buffer = FakeBuffer(
        recent=[candidate],
        create_error=buffer_x_publish.BufferXDeliveryUncertainError("timed out"),
    )

    result = _create(s3, buffer, uid="x:strict-match")

    assert result.record.status == "uncertain"
    assert len(buffer.create_calls) == 1


def test_crashed_attempting_invocation_is_reconciled_without_create_post():
    s3 = FakeS3()
    uid = "x:crashed-attempt"
    asyncio.run(
        prepare_x_delivery(uid, TEXT, IMAGE_URLS, client=s3, bucket="bucket", now=NOW, channel_id="channel-x")
    )
    attempting = asyncio.run(
        claim_x_delivery_create(uid, client=s3, bucket="bucket", now=NOW, channel_id="channel-x")
    )
    assert attempting is not None and attempting.record.status == "attempting"
    buffer = FakeBuffer(recent=[_post("scheduled")])

    again = _create(s3, buffer, uid=uid, now=NOW + timedelta(days=30))
    assert again.record.status == "attempting"
    assert buffer.create_calls == []

    recovered = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=1),
        )
    )
    assert recovered[0].record.status == "accepted"
    assert buffer.create_calls == []


def test_prepared_record_after_crash_before_attempting_can_be_claimed_once():
    s3 = FakeS3()
    uid = "x:crash-before-attempt"
    asyncio.run(
        prepare_x_delivery(uid, TEXT, IMAGE_URLS, client=s3, bucket="bucket", now=NOW, channel_id="channel-x")
    )
    buffer = FakeBuffer()

    first = _create(s3, buffer, uid=uid, now=NOW + timedelta(minutes=1))
    second = _create(s3, buffer, uid=uid, now=NOW + timedelta(minutes=2))

    assert first.record.status == "accepted"
    assert second.record.status == "accepted"
    assert len(buffer.create_calls) == 1


def test_successful_create_before_buffer_id_write_is_recovered_without_second_create():
    s3 = FailOnceOnStatusS3("accepted")
    buffer = FakeBuffer(recent=[_post("scheduled")])
    uid = "x:create-before-id-write"

    with pytest.raises(StorageUnavailableError, match="X delivery transition failed"):
        _create(s3, buffer, uid=uid)
    after_error = asyncio.run(get_x_delivery(uid, client=s3, bucket="bucket", channel_id="channel-x"))
    assert after_error is not None and after_error.record.status == "attempting"

    recovered = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=1),
        )
    )
    assert recovered[0].record.status == "accepted"
    assert recovered[0].record.buffer_post_id == "buffer-1"
    assert len(buffer.create_calls) == 1


def test_accepted_state_survives_lost_invocation_response(monkeypatch):
    s3 = FakeS3()
    buffer = FakeBuffer()
    original = x_delivery.mark_x_delivery_accepted

    async def persist_then_lose_response(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("process stopped after accepted was persisted")

    monkeypatch.setattr(
        x_delivery,
        "mark_x_delivery_accepted",
        persist_then_lose_response,
    )
    with pytest.raises(RuntimeError, match="after accepted was persisted"):
        _create(s3, buffer, uid="x:accepted-response-lost")

    persisted = asyncio.run(
        get_x_delivery("x:accepted-response-lost", client=s3, bucket="bucket", channel_id="channel-x")
    )
    assert persisted is not None and persisted.record.status == "accepted"

    replay = _create(s3, buffer, uid="x:accepted-response-lost", now=NOW + timedelta(minutes=1))
    assert replay.record.status == "accepted"
    assert len(buffer.create_calls) == 1


def test_buffer_sent_before_sent_state_write_is_safely_rechecked():
    s3 = FailOnceOnStatusS3("sent")
    link = "https://x.com/cs2results/status/456"
    buffer = FakeBuffer(posts={"buffer-1": _post("sent", link)})
    _create(s3, buffer, uid="x:sent-before-storage-write")

    with pytest.raises(StorageUnavailableError, match="X delivery transition failed"):
        asyncio.run(
            check_due_x_deliveries(
                "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
                now=NOW + timedelta(minutes=5),
            )
        )
    state_after_error = asyncio.run(
        get_x_delivery("x:sent-before-storage-write", client=s3, bucket="bucket", channel_id="channel-x")
    )
    assert state_after_error is not None and state_after_error.record.status == "accepted"

    sent = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=6),
        )
    )
    assert sent[0].record.status == "sent"
    assert sent[0].record.x_url == link
    assert len(buffer.create_calls) == 1


def test_due_check_limit_bounds_buffer_requests():
    s3 = FakeS3()
    buffer = FakeBuffer(posts={"buffer-1": _post("scheduled")})
    for index in range(4):
        uid = f"x:limit:{index}"
        _create(s3, buffer, uid=uid)
        # Give each accepted record the same Buffer ID for this bounded-call test.
    outcomes = asyncio.run(
        check_due_x_deliveries(
            "test-key", "org-1", client=s3, bucket="bucket", buffer_api=buffer,
            now=NOW + timedelta(minutes=5), max_requests=2,
        )
    )
    assert len(outcomes) == 2
    assert len(buffer.get_calls) == 2
