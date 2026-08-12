import asyncio
import json
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from cs2bot.match_sources.models import MatchNormalized
from cs2bot.match_sources.storage import (
    alert_key,
    claim_admin_alert,
    claim_channel_delivery,
    claim_content_delivery,
    channel_match_uid,
    is_channel_processed,
    is_processed,
    legacy_channel_match_uid,
    mark_channel_processed,
    mark_content_processed,
    mark_processed,
    processed_key,
    release_delivery_claim,
)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.version = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        item = self.objects[Key]
        return {"Metadata": item.get("Metadata", {}), "ETag": item["ETag"]}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None, IfNoneMatch=None, IfMatch=None):
        existing = self.objects.get(Key)
        if IfNoneMatch == "*" and existing is not None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        if IfMatch and (existing is None or existing["ETag"] != IfMatch):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.version += 1
        etag = f'"etag-{self.version}"'
        self.objects[Key] = {
            "Body": Body,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
            "ETag": etag,
        }
        return {"ETag": etag}


def _match():
    return MatchNormalized(
        source="hltv",
        match_id="2378481",
        match_url="https://www.hltv.org/matches/2378481/test",
        tournament_name="IEM Cologne 2026",
        team1_name="NAVI",
        team2_name="FaZe",
        score1=2,
        score2=1,
        start_date="2026-02-17T10:30:00Z",
        end_date="2026-02-17T12:40:00Z",
    )


def test_is_processed_false_when_object_missing():
    s3 = FakeS3()
    assert asyncio.run(is_processed("hltv_2378481", client=s3, bucket="bucket")) is False


def test_mark_processed_creates_object():
    s3 = FakeS3()
    match = _match()
    asyncio.run(mark_processed(match, client=s3, bucket="bucket"))
    key = processed_key(match.match_uid)
    assert key in s3.objects
    payload = json.loads(s3.objects[key]["Body"])
    assert payload["match_uid"] == match.match_uid
    assert payload["start_date"] == "2026-02-17T10:30:00Z"
    assert payload["end_date"] == "2026-02-17T12:40:00Z"


def test_is_processed_true_after_mark_processed():
    s3 = FakeS3()
    match = _match()
    asyncio.run(mark_processed(match, client=s3, bucket="bucket"))
    assert asyncio.run(is_processed(match.match_uid, client=s3, bucket="bucket")) is True


def test_channel_processed_uses_channel_specific_uid():
    s3 = FakeS3()
    match = _match()
    asyncio.run(mark_channel_processed(match, "global", client=s3, bucket="bucket"))
    uid = channel_match_uid(match, "global")
    assert uid.startswith("global_match_v1_")
    assert asyncio.run(is_processed(uid, client=s3, bucket="bucket")) is True


def test_channel_processed_accepts_legacy_source_specific_key():
    s3 = FakeS3()
    match = _match()
    legacy_uid = legacy_channel_match_uid(match, "global")
    s3.put_object(
        Bucket="bucket",
        Key=processed_key(legacy_uid),
        Body=b"{}",
        ContentType="application/json",
    )

    assert asyncio.run(
        is_channel_processed(match, "global", client=s3, bucket="bucket")
    ) is True


def test_delivery_claim_prevents_concurrent_publication_and_can_be_released():
    s3 = FakeS3()
    match = _match()
    now = datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)

    first = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )
    second = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )

    assert first is not None
    assert second is None

    asyncio.run(release_delivery_claim(first, client=s3, bucket="bucket"))
    third = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )
    assert third is not None
    assert third.claim_id != first.claim_id


def test_expired_delivery_claim_is_atomically_reclaimed():
    s3 = FakeS3()
    match = _match()
    now = datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)

    first = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )
    reclaimed = asyncio.run(
        claim_channel_delivery(
            match,
            "global",
            client=s3,
            bucket="bucket",
            now=now + timedelta(minutes=6),
        )
    )

    assert first is not None
    assert reclaimed is not None
    assert reclaimed.claim_id != first.claim_id
    assert reclaimed.etag != first.etag


def test_admin_alert_is_claimed_only_once_per_cooldown_window():
    s3 = FakeS3()
    now = datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)

    assert asyncio.run(claim_admin_alert("source-down", client=s3, bucket="bucket", now=now))
    assert not asyncio.run(claim_admin_alert("source-down", client=s3, bucket="bucket", now=now))
    assert alert_key("source-down", now) in s3.objects


def test_content_delivery_is_atomic_and_persisted_after_success():
    s3 = FakeS3()
    now = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)

    first = asyncio.run(
        claim_content_delivery("schedule_2026-07-30_global", client=s3, bucket="bucket", now=now)
    )
    second = asyncio.run(
        claim_content_delivery("schedule_2026-07-30_global", client=s3, bucket="bucket", now=now)
    )
    assert first is not None
    assert second is None

    asyncio.run(
        mark_content_processed(
            "schedule_2026-07-30_global",
            "schedule",
            client=s3,
            bucket="bucket",
        )
    )
    asyncio.run(release_delivery_claim(first, client=s3, bucket="bucket"))
    assert asyncio.run(
        claim_content_delivery("schedule_2026-07-30_global", client=s3, bucket="bucket", now=now)
    ) is None
