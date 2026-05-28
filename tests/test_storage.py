import asyncio
import json

from botocore.exceptions import ClientError

from cs2bot.match_sources.models import MatchNormalized
from cs2bot.match_sources.storage import (
    channel_match_uid,
    is_processed,
    mark_channel_processed,
    mark_processed,
    processed_key,
)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = {"Body": Body, "ContentType": ContentType}
        return {}


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
    assert uid == "global_hltv_2378481"
    assert asyncio.run(is_processed(uid, client=s3, bucket="bucket")) is True
