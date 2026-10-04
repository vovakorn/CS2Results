import copy
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
import requests

from cs2bot import social_oauth


@pytest.fixture
def renewal(monkeypatch):
    for key, value in {
        "THREADS_APP_ID": "app-id", "THREADS_APP_SECRET": "app-secret",
        "THREADS_EXPECTED_USER_ID": "789", "THREADS_LOCKBOX_SECRET_ID": "secret-id",
        "SOCIAL_OAUTH_EXPECTED_USERNAME": "cs2results",
    }.items():
        monkeypatch.setenv(key, value)
    now = datetime.now(timezone.utc)
    state = {
        "secret": {"versionId": "original", "entries": [
            {"key": "ACCESS_TOKEN", "textValue": "old-private-token"},
            {"key": "USER_ID", "textValue": "789"},
            {"key": "TOKEN_EXPIRES_AT", "textValue": (now + timedelta(days=10)).isoformat()},
            {"key": "APP_SECRET", "textValue": "saved-app-secret"},
            {"key": "EXTRA", "binaryValue": "aGVsbG8="},
        ]},
        "meta": [], "writes": [], "reads": 0,
        "current": {"is_valid": True, "user_id": "789", "app_id": "app-id",
                    "issued_at": int((now - timedelta(days=50)).timestamp()),
                    "expires_at": int((now + timedelta(days=10)).timestamp()),
                    "scopes": social_oauth.THREADS_OAUTH_SCOPES.split(",")},
        "new": {"is_valid": True, "user_id": "789", "app_id": "app-id",
                "expires_at": int((now + timedelta(days=60)).timestamp()),
                "scopes": social_oauth.THREADS_OAUTH_SCOPES.split(",")},
        "refresh": {"access_token": "new-private-token", "expires_in": 60 * 86400},
        "profile": {"id": "789", "username": "cs2results"},
        "operation": {"done": True, "response": {"id": "renewed"}},
    }
    class Response:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return copy.deepcopy(self.payload)
        def raise_for_status(self): pass
    def get(url, **kwargs):
        assert url == "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets/secret-id/payload"
        assert "proxies" not in kwargs
        state["reads"] += 1
        payload = copy.deepcopy(state["secret"])
        if state.get("changed") and state["reads"] > 1:
            payload["versionId"] = "new-owner-authorization"
        return Response(payload)
    def request(method, url, **kwargs):
        assert kwargs["proxies"] == {"https": "proxy"}
        state["meta"].append(url)
        if state.get("transport_error"):
            raise requests.Timeout("url?access_token=old-private-token")
        if url.endswith("/debug_token"):
            return Response({"data": state["current"] if kwargs["params"]["input_token"] == "old-private-token" else state["new"]})
        if url.endswith("/refresh_access_token"):
            assert kwargs["params"]["grant_type"] == "th_refresh_token"
            return Response(state["refresh"])
        if url.endswith("/me"):
            return Response(state["profile"])
        raise AssertionError("unexpected Meta request")
    def post(url, **kwargs):
        assert ":addVersion" in url
        assert "proxies" not in kwargs
        state["writes"].append(kwargs["json"])
        return Response(state["operation"])
    monkeypatch.setattr(social_oauth.requests, "get", get)
    monkeypatch.setattr(social_oauth.requests, "request", request)
    monkeypatch.setattr(social_oauth.requests, "post", post)
    monkeypatch.setattr(social_oauth, "xray_http_proxy", lambda: nullcontext({"https": "proxy"}))
    return state


def run_job(**kwargs):
    return social_oauth.handler({"internal_job": "threads_token_refresh", **kwargs}, {"token": "iam-token"})


def test_fresh_token_skips_meta_and_lockbox_write(renewal):
    renewal["secret"]["entries"][2]["textValue"] = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    response = run_job()
    assert json.loads(response["body"])["outcome"] == "not_due"
    assert not renewal["meta"] and not renewal["writes"]


def test_due_token_checks_identity_permissions_and_preserves_base_entries(renewal):
    response = run_job()
    assert json.loads(response["body"]) == {"ok": True, "outcome": "refreshed"}
    assert len(renewal["writes"]) == 1
    write = renewal["writes"][0]
    assert write["baseVersionId"] == "original"
    updates = {item["key"]: item["textValue"] for item in write["payloadEntries"]}
    assert set(updates) == {"ACCESS_TOKEN", "TOKEN_EXPIRES_AT", "GRANTED_SCOPES"}
    assert updates["ACCESS_TOKEN"] == "new-private-token"
    assert datetime.fromisoformat(updates["TOKEN_EXPIRES_AT"].replace("Z", "+00:00")).timestamp() <= renewal["new"]["expires_at"]
    assert set(updates["GRANTED_SCOPES"].split(",")) == set(social_oauth.THREADS_OAUTH_SCOPES.split(","))
    assert "private-token" not in response["body"]


def test_dry_run_never_refreshes_or_writes(renewal):
    response = run_job(dry_run=True)
    assert json.loads(response["body"])["outcome"] == "refresh_due"
    assert len(renewal["meta"]) == 1 and not renewal["writes"]


def test_invalid_dry_run_cannot_accidentally_refresh(renewal):
    assert run_job(dry_run="true")["statusCode"] == 503
    assert not renewal["meta"] and not renewal["writes"]


def test_completed_renewal_is_skipped_on_next_daily_invocation(renewal):
    assert run_job()["statusCode"] == 200
    values = {item["key"]: item for item in renewal["secret"]["entries"]}
    values.update({item["key"]: item for item in renewal["writes"][0]["payloadEntries"]})
    renewal["secret"] = {"versionId": "renewed", "entries": list(values.values())}
    meta_calls = len(renewal["meta"])
    assert json.loads(run_job()["body"])["outcome"] == "not_due"
    assert len(renewal["writes"]) == 1 and len(renewal["meta"]) == meta_calls


@pytest.mark.parametrize("field,value", [("is_valid", False), ("scopes", ["threads_basic"]), ("user_id", "other"), ("app_id", "other"), ("expires_at", 1)])
def test_invalid_current_token_is_not_refreshed(renewal, field, value):
    renewal["current"][field] = value
    assert run_job()["statusCode"] == 503
    assert len(renewal["meta"]) == 1 and not renewal["writes"]


@pytest.mark.parametrize("field,value", [("is_valid", False), ("scopes", []), ("user_id", "other"), ("app_id", "other"), ("expires_at", 1)])
def test_invalid_refreshed_token_never_replaces_saved_token(renewal, field, value):
    renewal["new"][field] = value
    assert run_job()["statusCode"] == 503
    assert not renewal["writes"]


@pytest.mark.parametrize("lifetime", [0, -1, None, "5184000", True])
def test_invalid_refresh_lifetime_never_writes(renewal, lifetime):
    renewal["refresh"]["expires_in"] = lifetime
    assert run_job()["statusCode"] == 503
    assert not renewal["writes"]


@pytest.mark.parametrize("expiry", ["bad", "2026-01-01T00:00:00", "2000-01-01T00:00:00Z"])
def test_missing_or_expired_saved_lifetime_requires_owner_action(renewal, expiry):
    renewal["secret"]["entries"][2]["textValue"] = expiry
    assert run_job()["statusCode"] == 503
    assert not renewal["meta"] and not renewal["writes"]


def test_young_token_is_not_refreshed(renewal):
    renewal["current"]["issued_at"] = int(datetime.now(timezone.utc).timestamp()) - 3600
    assert json.loads(run_job()["body"])["outcome"] == "too_young"
    assert len(renewal["meta"]) == 1 and not renewal["writes"]


def test_changed_secret_is_not_overwritten(renewal):
    renewal["changed"] = True
    assert run_job()["statusCode"] == 503
    assert not renewal["writes"]


def test_wrong_account_profile_is_not_written(renewal):
    renewal["profile"]["username"] = "other-account"
    assert run_job()["statusCode"] == 503
    assert not renewal["writes"]


def test_transport_error_does_not_log_or_return_token(renewal, caplog):
    renewal["transport_error"] = True
    response = run_job()
    assert response["statusCode"] == 503
    assert "private-token" not in caplog.text + response["body"]
    assert not renewal["writes"]


def timer_event():
    return {"messages": [{"event_metadata": {"event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"},
                          "details": {"payload": json.dumps({"internal_job": "threads_token_refresh"})}}]}


def test_timer_dispatches_and_failure_is_not_a_successful_invocation(renewal):
    assert social_oauth.handler(timer_event(), {"token": "iam-token"})["statusCode"] == 200
    renewal["current"]["is_valid"] = False
    with pytest.raises(social_oauth.OAuthFlowError, match="renewal failed"):
        social_oauth.handler(timer_event(), {"token": "iam-token"})


def test_http_route_cannot_dispatch_refresh(renewal):
    response = run_job(path="/oauth/meta/threads/refresh", httpMethod="POST")
    assert response["statusCode"] == 404
    assert not renewal["meta"] and not renewal["writes"]


def test_pending_lockbox_operation_is_not_reported_as_renewed(renewal):
    renewal["operation"] = {"done": False, "id": "pending-operation"}
    assert run_job()["statusCode"] == 503
