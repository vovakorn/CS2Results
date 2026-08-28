import asyncio
import io
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
    clear_telegram_media_degraded,
    delete_result_delivery,
    enqueue_result_delivery,
    is_channel_processed,
    is_processed,
    is_telegram_media_degraded,
    legacy_channel_match_uid,
    list_pending_result_deliveries,
    logo_cache_key,
    mark_channel_processed,
    mark_content_processed,
    mark_delivery_claim_sent,
    mark_telegram_media_degraded,
    mark_processed,
    processed_key,
    record_result_delivery_attempt,
    release_delivery_claim,
    reconcile_channel_delivery,
    reconcile_content_delivery,
    read_cached_logo,
    StorageUnavailableError,
    write_cached_logo,
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

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {
            "Contents": [{"Key": key} for key in keys],
            "IsTruncated": False,
        }

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}


class TitleCaseMetadataS3(FakeS3):
    """Mirror Yandex Object Storage metadata spelling observed in production."""

    def head_object(self, Bucket, Key):
        response = super().head_object(Bucket=Bucket, Key=Key)
        response["Metadata"] = {
            "-".join(part.capitalize() for part in key.split("-")): value
            for key, value in response["Metadata"].items()
        }
        return response


class FlakySentStateS3(FakeS3):
    def __init__(self):
        super().__init__()
        self.sent_state_failures = 0

    def put_object(self, *args, **kwargs):
        metadata = kwargs.get("Metadata")
        if metadata and metadata.get("delivery-state") == "sent" and not self.sent_state_failures:
            self.sent_state_failures += 1
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
                "PutObject",
            )
        return super().put_object(*args, **kwargs)


class FakeUnquotedETagS3(FakeS3):
    """Emulate an S3-compatible endpoint that compares ETags without quotes."""

    def put_object(self, *args, IfMatch=None, **kwargs):
        if IfMatch:
            if IfMatch.startswith('"'):
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )
            key = kwargs["Key"]
            existing = self.objects.get(key)
            if existing and existing["ETag"].strip('"') != IfMatch:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )
            original = existing["ETag"] if existing else None
            if existing:
                existing["ETag"] = IfMatch
            try:
                return super().put_object(*args, IfMatch=IfMatch, **kwargs)
            finally:
                if existing and original is not None:
                    existing["ETag"] = original
        return super().put_object(*args, IfMatch=IfMatch, **kwargs)


class FakeStaleHeadS3(FakeS3):
    """Return one stale ETag to verify reclaim refreshes the lease state."""

    def __init__(self):
        super().__init__()
        self.stale_head = True

    def head_object(self, Bucket, Key):
        result = super().head_object(Bucket, Key)
        if self.stale_head and Key in self.objects:
            self.stale_head = False
            result["ETag"] = '"stale-etag"'
        return result


class FakeAlwaysConflictS3(FakeS3):
    """Return conditional conflicts to ensure storage errors are not duplicates."""

    def put_object(self, *args, IfMatch=None, **kwargs):
        if IfMatch:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        return super().put_object(*args, IfMatch=IfMatch, **kwargs)


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


def test_logo_cache_round_trips_png_by_source_url():
    s3 = FakeS3()
    url = "https://cdn-api.pandascore.co/images/team/image/10/navi.png"
    raw = b"validated-logo"

    assert write_cached_logo(url, raw, client=s3, bucket="bucket") is True
    assert read_cached_logo(url, client=s3, bucket="bucket") == raw
    assert logo_cache_key(url) in s3.objects
    assert s3.objects[logo_cache_key(url)]["ContentType"] == "image/png"


def test_result_outbox_preserves_retry_state_and_deletes_after_success():
    s3 = FakeS3()
    match = _match()
    created = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

    assert asyncio.run(
        enqueue_result_delivery(
            match,
            "global",
            "Global",
            client=s3,
            bucket="bucket",
            now=created,
        )
    )
    pending = asyncio.run(
        list_pending_result_deliveries(client=s3, bucket="bucket")
    )[0]
    updated = asyncio.run(
        record_result_delivery_attempt(
            pending,
            client=s3,
            bucket="bucket",
            now=created + timedelta(minutes=5),
        )
    )

    assert updated.attempt_count == 1
    assert asyncio.run(
        enqueue_result_delivery(
            match,
            "global",
            "Global",
            client=s3,
            bucket="bucket",
            now=created + timedelta(minutes=10),
        )
    ) is False
    reloaded = asyncio.run(
        list_pending_result_deliveries(client=s3, bucket="bucket")
    )[0]
    assert reloaded.attempt_count == 1
    assert reloaded.created_at == "2026-08-28T08:00:00Z"

    asyncio.run(delete_result_delivery(reloaded, client=s3, bucket="bucket"))
    assert asyncio.run(
        list_pending_result_deliveries(client=s3, bucket="bucket")
    ) == []


def test_result_outbox_prioritizes_never_attempted_and_least_recent_items():
    s3 = FakeS3()
    first = _match()
    second = _match().model_copy(
        update={
            "match_id": "2378482",
            "match_url": "https://example.com/2",
            "team2_name": "MOUZ",
        }
    )
    created = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    asyncio.run(enqueue_result_delivery(first, "global", "Global", client=s3, bucket="bucket", now=created))
    asyncio.run(
        enqueue_result_delivery(
            second,
            "global",
            "Global",
            client=s3,
            bucket="bucket",
            now=created + timedelta(seconds=1),
        )
    )
    queued = asyncio.run(list_pending_result_deliveries(client=s3, bucket="bucket"))
    asyncio.run(
        record_result_delivery_attempt(
            queued[0],
            client=s3,
            bucket="bucket",
            now=created + timedelta(minutes=1),
        )
    )

    reordered = asyncio.run(list_pending_result_deliveries(client=s3, bucket="bucket"))
    assert reordered[0].match.match_id == "2378482"
    assert reordered[1].attempt_count == 1


def test_telegram_media_degraded_window_expires_and_can_be_cleared():
    s3 = FakeS3()
    now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

    assert asyncio.run(
        is_telegram_media_degraded(client=s3, bucket="bucket", now=now)
    ) is False
    asyncio.run(
        mark_telegram_media_degraded(
            now + timedelta(hours=1), client=s3, bucket="bucket"
        )
    )
    assert asyncio.run(
        is_telegram_media_degraded(client=s3, bucket="bucket", now=now)
    ) is True
    assert asyncio.run(
        is_telegram_media_degraded(
            client=s3, bucket="bucket", now=now + timedelta(hours=2)
        )
    ) is False
    asyncio.run(clear_telegram_media_degraded(client=s3, bucket="bucket"))
    assert asyncio.run(
        is_telegram_media_degraded(client=s3, bucket="bucket", now=now)
    ) is False


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


def test_confirmed_delivery_is_reconciled_without_a_second_claim():
    s3 = FakeS3()
    match = _match()
    now = datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)
    claim = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )

    assert claim is not None
    asyncio.run(mark_delivery_claim_sent(claim, client=s3, bucket="bucket"))
    assert asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    ) is None
    assert asyncio.run(reconcile_channel_delivery(match, "global", client=s3, bucket="bucket"))
    assert asyncio.run(is_channel_processed(match, "global", client=s3, bucket="bucket"))


def test_confirmed_delivery_retries_transient_sent_state_write():
    s3 = FlakySentStateS3()
    match = _match()
    claim = asyncio.run(claim_channel_delivery(match, "global", client=s3, bucket="bucket"))

    assert claim is not None
    asyncio.run(mark_delivery_claim_sent(claim, client=s3, bucket="bucket"))

    assert s3.sent_state_failures == 1
    assert s3.objects[claim.key]["Metadata"]["delivery-state"] == "sent"


def test_confirmed_content_delivery_is_reconciled_without_a_second_claim():
    s3 = FakeS3()
    content_uid = "schedule_2026-07-30_global"
    claim = asyncio.run(claim_content_delivery(content_uid, client=s3, bucket="bucket"))

    assert claim is not None
    asyncio.run(mark_delivery_claim_sent(claim, client=s3, bucket="bucket"))
    assert asyncio.run(
        reconcile_content_delivery(content_uid, "schedule", client=s3, bucket="bucket")
    )
    assert asyncio.run(is_processed(f"content_{content_uid}", client=s3, bucket="bucket"))


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


def test_expired_delivery_claim_accepts_title_case_metadata_from_yandex_storage():
    s3 = TitleCaseMetadataS3()
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


def test_confirmed_delivery_reconciles_title_case_metadata_from_yandex_storage():
    s3 = TitleCaseMetadataS3()
    match = _match()
    claim = asyncio.run(claim_channel_delivery(match, "global", client=s3, bucket="bucket"))

    assert claim is not None
    asyncio.run(mark_delivery_claim_sent(claim, client=s3, bucket="bucket"))

    assert asyncio.run(reconcile_channel_delivery(match, "global", client=s3, bucket="bucket"))
    assert asyncio.run(is_channel_processed(match, "global", client=s3, bucket="bucket"))


def test_expired_delivery_claim_retries_unquoted_etag_for_compatible_s3_endpoint():
    s3 = FakeUnquotedETagS3()
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


def test_expired_delivery_claim_refreshes_etag_after_conditional_conflict():
    s3 = FakeStaleHeadS3()
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


def test_expired_delivery_claim_conflict_is_reported_as_storage_error():
    s3 = FakeAlwaysConflictS3()
    match = _match()
    now = datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)

    first = asyncio.run(
        claim_channel_delivery(match, "global", client=s3, bucket="bucket", now=now)
    )
    assert first is not None

    try:
        asyncio.run(
            claim_channel_delivery(
                match,
                "global",
                client=s3,
                bucket="bucket",
                now=now + timedelta(minutes=6),
            )
        )
    except StorageUnavailableError as exc:
        assert "expired claim cannot be reclaimed" in str(exc)
    else:
        raise AssertionError("expired claim conflict must not be reported as a duplicate")


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
