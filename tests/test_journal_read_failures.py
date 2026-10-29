"""Use the real S3 client to verify admin journal failures cannot become zeros."""
import asyncio
import io
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from cs2bot import admin_handler as admin, publication_admin as journal
from cs2bot.match_sources.storage import StorageUnavailableError


NOW = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
SINCE = journal.period_start("today", now=NOW)
KEY = f"{journal.JOURNAL_PREFIX}2026-10-29/confirmed.json"


@pytest.fixture
def s3():
    # Explicit dummy credentials keep the client away from metadata/network.
    client = boto3.client(
        "s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1",
        aws_access_key_id="offline", aws_secret_access_key="offline",
    )
    yield client
    client.close()


def add_listing(stub, keys):
    stub.add_response("list_objects_v2", {
        "Contents": [{"Key": key, "LastModified": NOW} for key in keys], "IsTruncated": False,
    }, {"Bucket": "offline", "Prefix": journal.JOURNAL_PREFIX, "MaxKeys": 1000,
        "StartAfter": f"{journal.JOURNAL_PREFIX}{SINCE.astimezone(timezone.utc).date().isoformat()}/"})


@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("ServiceUnavailable", 503)])
def test_journal_body_permission_or_service_failure_is_not_empty_history(s3, code, status):
    with Stubber(s3) as stub:
        add_listing(stub, [KEY])
        stub.add_client_error("get_object", service_error_code=code, http_status_code=status,
                              expected_params={"Bucket": "offline", "Key": KEY})
        with pytest.raises(StorageUnavailableError, match="journal item read failed"):
            asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE))
        stub.assert_no_pending_responses()


def test_journal_skips_a_confirmed_not_found_after_listing(s3):
    with Stubber(s3) as stub:
        add_listing(stub, [KEY])
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404,
                              expected_params={"Bucket": "offline", "Key": KEY})
        assert asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE)) == []
        stub.assert_no_pending_responses()


def test_mixed_success_and_error_cannot_return_partial_confirmed_counts(s3):
    with Stubber(s3) as stub:
        add_listing(stub, [KEY, KEY.replace("confirmed", "other")])
        record = {"publication_id": "visible", "destination_id": "global", "publication_type": "results",
                  "confirmed_at": NOW.isoformat()}
        # Concurrent GET order is immaterial: one valid body and one service
        # error must fail the complete read, regardless of which key gets each.
        stub.add_response("get_object", {"Body": io.BytesIO(json.dumps(record).encode())})
        stub.add_client_error("get_object", service_error_code="ServiceUnavailable", http_status_code=503)
        with pytest.raises(StorageUnavailableError, match="journal item read failed"):
            asyncio.run(journal.list_publications(client=s3, bucket="offline", since=SINCE))
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("ServiceUnavailable", 503)])
def test_admin_overview_returns_error_instead_of_zero_on_body_read_failure(monkeypatch, s3, code, status):
    monkeypatch.setattr(journal, "_client", lambda: s3)
    monkeypatch.setattr(journal, "_bucket", lambda: "offline")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "offline-secret")
    monkeypatch.setattr(admin, "period_start", lambda period: SINCE)
    monkeypatch.setattr(admin, "telegram_call", Mock())
    sent = Mock()
    monkeypatch.setattr(admin, "_send_or_edit", sent)
    event = {
        "headers": {"X-Telegram-Bot-Api-Secret-Token": "offline-secret"},
        "body": json.dumps({"callback_query": {
            "id": "offline-callback", "from": {"id": 42}, "data": "a:o:today",
            "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}},
        }}),
    }
    with Stubber(s3) as stub:
        stub.add_response("get_object", {
            "Body": io.BytesIO(json.dumps({"version": 1, "revision": 1, "entries": {}}).encode()),
            "ETag": '"1"',
        }, {"Bucket": "offline", "Key": journal.POLICY_KEY})
        add_listing(stub, [KEY])
        stub.add_client_error("get_object", service_error_code=code, http_status_code=status,
                              expected_params={"Bucket": "offline", "Key": KEY})
        response = admin.handler(event, None)
        stub.assert_no_pending_responses()
    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {"ok": False}
    sent.assert_not_called()
