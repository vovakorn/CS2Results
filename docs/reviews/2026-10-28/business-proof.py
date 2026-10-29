"""Offline review probes. No external requests and no repository mutations.

Usage: <project-python> proof.py /absolute/path/to/current/repository
Exit 0 means the observed behavior matches the review evidence, not correctness.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from botocore.exceptions import ClientError
from cs2bot.match_sources.filters import is_tier1_lan
from cs2bot.match_sources.models import MatchNormalized
from cs2bot.match_sources.storage import (
    claim_content_delivery, enqueue_result_delivery, is_channel_processed,
    list_pending_result_deliveries, mark_channel_processed,
)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.version = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        item = self.objects[Key]
        return {"Metadata": item["Metadata"], "ETag": item["ETag"]}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None,
                   IfNoneMatch=None, IfMatch=None):
        existing = self.objects.get(Key)
        if ((IfNoneMatch == "*" and existing is not None)
                or (IfMatch and (existing is None or existing["ETag"] != IfMatch))):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.version += 1
        etag = f'"etag-{self.version}"'
        self.objects[Key] = {"Body": Body, "ContentType": ContentType,
                             "Metadata": Metadata or {}, "ETag": etag}
        return {"ETag": etag}

    def get_object(self, Bucket, Key):
        self.head_object(Bucket, Key)
        item = self.objects[Key]
        return {"Body": io.BytesIO(item["Body"]), "ETag": item["ETag"]}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def match(**overrides):
    data = dict(source="pandascore", match_id="x", tournament_name="IEM Cologne 2026",
                competition_key="IEM Cologne 2026", team1_name="NAVI", team2_name="FaZe",
                score1=2, score2=1, best_of=3)
    data.update(overrides)
    return MatchNormalized(**data)


async def race_probe():
    class RaceS3(FakeS3):
        armed = False

        def put_object(self, Bucket, Key, Body, ContentType, Metadata=None,
                       IfNoneMatch=None, IfMatch=None):
            if self.armed and IfMatch:
                self.armed = False
                # Another invocation acquired the expired lease and entered
                # attempting between our HEAD and conditional reclaim PUT.
                super().put_object(Bucket, Key, b"{}", ContentType,
                    Metadata={"delivery-state": "attempting", "expires-at": "0",
                              "claim-id": "other-invocation"})
            return super().put_object(Bucket, Key, Body, ContentType,
                                      Metadata, IfNoneMatch, IfMatch)

    s3 = RaceS3()
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    first = await claim_content_delivery("race-proof", client=s3, bucket="b", now=now)
    s3.armed = True
    second = await claim_content_delivery("race-proof", client=s3, bucket="b",
                                         now=now + timedelta(hours=1))
    result = {"other_attempting_claim_overwritten": second is not None,
              "final_state": s3.objects[first.key]["Metadata"]["delivery-state"]}
    assert result == {"other_attempting_claim_overwritten": True, "final_state": "sending"}
    return result


async def starvation_probe():
    s3 = FakeS3()
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    for index in range(201):
        await enqueue_result_delivery(match(match_id=str(index)), "global", "global",
            client=s3, bucket="b", now=now + timedelta(seconds=index),
            generation=0 if index < 200 else 1)
    first = await list_pending_result_deliveries(client=s3, bucket="b", limit=200)
    # main's owner-policy branch does not delete/update these held objects.
    second = await list_pending_result_deliveries(client=s3, bucket="b", limit=200)
    result = {"loaded": len(first), "eligible_generation_1": sum(p.generation >= 1 for p in first),
              "same_items_on_next_retry": [p.key for p in first] == [p.key for p in second]}
    assert result == {"loaded": 200, "eligible_generation_1": 0, "same_items_on_next_retry": True}
    return result


async def midnight_probe():
    common = dict(start_date="2026-10-28T23:30:00Z")
    primary = match(date="2026-10-29T01:30:00Z", end_date="2026-10-29T01:30:00Z", **common)
    lp = match(source="liquipedia", match_id="lp", date="2026-10-28T23:30:00Z",
               is_final=True, final_identity_confirmed=True, **common)
    s3 = FakeS3()
    await mark_channel_processed(lp, "test", client=s3, bucket="b")
    result = {"canonical_uid_equal": primary.match_uid == lp.match_uid,
              "bridge_uid_equal": primary.final_bridge_uid == lp.final_bridge_uid,
              "late_primary_seen_processed": await is_channel_processed(primary, "test", client=s3, bucket="b")}
    assert result == {"canonical_uid_equal": False, "bridge_uid_equal": False,
                      "late_primary_seen_processed": False}
    return result


def online_probe():
    common = dict(tournament_name="BLAST Bounty — 2026 Season 2 — Online Stage")
    unknown = is_tier1_lan(match(**common))
    explicit = is_tier1_lan(match(source="liquipedia", is_lan=False, **common))
    result = {"unknown_venue": unknown, "explicit_online": explicit,
              "qualification": "documented behavior: requirements says is_lan=false always rejected"}
    assert unknown == (True, None) and explicit == (False, "explicitly_not_lan")
    return result


async def main():
    result = {"claim_reclaim_race": await race_probe(),
              "held_queue_starvation": await starvation_probe(),
              "cross_midnight_dedup": await midnight_probe(),
              "online_spec_consistency": online_probe()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


asyncio.run(main())
