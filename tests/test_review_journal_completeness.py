"""The bounded journal reports unread candidates independently of body filters."""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from cs2bot import admin_handler as admin, publication_admin as journal


NOW = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
SINCE = journal.period_start("today", now=NOW)


class JournalS3:
    def __init__(self, count):
        self.records = {}
        self.reads = []
        for index in range(count):
            key = f"{journal.JOURNAL_PREFIX}2026-10-29/record-{index:04d}.json"
            payload = {"publication_id": f"publication-{index}", "destination_id": "global",
                       "publication_type": "results", "confirmed_at": (NOW + timedelta(seconds=index)).isoformat()}
            self.records[key] = {"body": json.dumps(payload).encode(), "modified": NOW + timedelta(seconds=index)}

    def list_objects_v2(self, **kwargs):
        keys = sorted(key for key in self.records if key.startswith(kwargs["Prefix"])
                      and key > kwargs.get("StartAfter", ""))
        return {"Contents": [{"Key": key, "LastModified": self.records[key]["modified"]} for key in keys],
                "IsTruncated": False}

    def get_object(self, Bucket, Key):
        self.reads.append(Key)
        body = self.records[Key]["body"]
        if body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(body)}


@pytest.fixture(autouse=True)
def fixed_display_period(monkeypatch):
    monkeypatch.setattr(admin, "publications_in_period", lambda records, period:
                        journal.publications_in_period(records, period, now=NOW))


def texts(records):
    policy = journal.PolicySnapshot(1, {}, None)
    return (admin._overview(records, [], policy, "today"),
            admin._journal_text(records, [], policy, "today", 0),
            admin._pair_detail(records, [], policy, "global", "results"))


@pytest.mark.parametrize("filtered", ["malformed", "404", "out_of_period", "test", "mixed"])
def test_unread_tail_is_disclosed_after_selected_bodies_are_filtered(filtered):
    s3 = JournalS3(501)
    last = f"{journal.JOURNAL_PREFIX}2026-10-29/record-0500.json"
    payload = json.loads(s3.records[last]["body"])
    if filtered == "malformed":
        s3.records[last]["body"] = b"{invalid"
    elif filtered == "404":
        s3.records[last]["body"] = None
    elif filtered == "out_of_period":
        payload["confirmed_at"] = (SINCE - timedelta(seconds=1)).isoformat()
        s3.records[last]["body"] = json.dumps(payload).encode()
    else:
        payload["test"] = True
        s3.records[last]["body"] = json.dumps(payload).encode()
        if filtered == "mixed":
            s3.records[last.replace("0500", "0499")]["body"] = b"{invalid"
            s3.records[last.replace("0500", "0498")]["body"] = None

    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE))
    assert len(s3.reads) == 500  # Never refill rejected bodies beyond the existing GET budget.
    assert records.unexamined_count == 1
    assert len(records) < 500 or filtered == "test"
    assert all(record["publication_id"] != "publication-0" for record in records)
    visible = journal.publications_in_period(records, "today", now=NOW)
    assert len(visible) < 500
    for text in texts(records):
        assert "могут быть неполными" in text


def test_exactly_500_examined_records_do_not_imply_an_unread_tail():
    s3 = JournalS3(500)
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE))
    assert len(records) == len(s3.reads) == 500
    assert records.unexamined_count == 0
    assert all("могут быть неполными" not in text for text in texts(records))


def test_empty_filtered_result_keeps_its_unread_tail_metadata():
    s3 = JournalS3(501)
    for key in sorted(s3.records)[1:]:
        s3.records[key]["body"] = b"{invalid"
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE))
    assert records == [] and isinstance(records, list)
    assert json.dumps(records) == "[]"  # Existing list callers/serialisation remain compatible.
    assert records.unexamined_count == 1
    assert len(s3.reads) == 500
    assert all("могут быть неполными" in text for text in texts(records))


def test_plain_list_length_alone_does_not_assert_truncation():
    assert admin._journal_limit_note([{}] * 500) == ""
