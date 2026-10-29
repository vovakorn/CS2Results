"""Read-only re-audit proofs. Tests assert defects observed before their fixes."""
import asyncio
import io
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import boto3
import pytest
import requests
from botocore.stub import Stubber

from cs2bot import admin_handler as admin, main, publication_admin as journal


def s3_client():
    return boto3.client(
        "s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1",
        aws_access_key_id="offline", aws_secret_access_key="offline",
    )


@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("ServiceUnavailable", 503)])
def test_body_read_service_error_is_silently_presented_as_zero_publications(code, status):
    client = s3_client()
    now = datetime.now(timezone.utc)
    since = journal.period_start("today", now=now)
    key = f"{journal.JOURNAL_PREFIX}{now.date().isoformat()}/existing-confirmed-post.json"
    with Stubber(client) as stub:
        stub.add_response("list_objects_v2", {
            "Contents": [{"Key": key, "LastModified": now}], "IsTruncated": False,
        }, {"Bucket": "offline", "Prefix": journal.JOURNAL_PREFIX, "MaxKeys": 1000,
            "StartAfter": f"{journal.JOURNAL_PREFIX}{since.astimezone(timezone.utc).date().isoformat()}/"})
        stub.add_client_error("get_object", service_error_code=code,
                              service_message="offline service failure", http_status_code=status,
                              expected_params={"Bucket": "offline", "Key": key})
        records = asyncio.run(journal.list_publications(client=client, bucket="offline", since=since))
        stub.assert_no_pending_responses()
    assert records == []
    text = admin._overview(records, [], journal.PolicySnapshot(1, {}, None), "today")
    assert "Подтверждённые публикации: <b>0</b>" in text
    assert "могут быть неполными" not in text


def test_successful_owner_allowed_schedule_context_never_records_a_publication(monkeypatch):
    client = s3_client()
    policy = {"version": 1, "revision": 2, "entries": {
        "global:schedule_context": {"enabled": True, "generation": 1},
    }}
    sent = []

    def post(url, **kwargs):
        sent.append((url.rsplit("/", 1)[-1], kwargs["json"]["text"]))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"ok":true,"result":{"message_id":7}}'
        return response

    monkeypatch.setattr(journal, "_client", lambda: client)
    monkeypatch.setattr(journal, "_bucket", lambda: "offline")
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "offline-token")
    monkeypatch.setattr(main, "TELEGRAM_PROXY_URL", None)
    monkeypatch.setattr(main.requests, "post", post)
    recorder = Mock(wraps=main._record_post_analytics)
    monkeypatch.setattr(main, "_record_post_analytics", recorder)
    with Stubber(client) as stub:
        stub.add_response("get_object", {"Body": io.BytesIO(json.dumps(policy).encode()), "ETag": '"2"'},
                          {"Bucket": "offline", "Key": journal.POLICY_KEY})
        main._send_schedule_context_to_telegram(
            {"id": "global", "chat_id": "offline-chat"}, ["Confirmed schedule context"], [],
            datetime.now(timezone.utc), None,
        )
        stub.assert_no_pending_responses()
    assert sent == [("sendMessage", "Confirmed schedule context")]
    recorder.assert_not_called()
