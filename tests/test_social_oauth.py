import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import pytest

from cs2bot import social_oauth


@pytest.fixture(autouse=True)
def oauth_environment(monkeypatch):
    values = {
        "SOCIAL_OAUTH_BASE_URL": "https://oauth.example.test",
        "SOCIAL_OAUTH_EXPECTED_USERNAME": "cs2results",
        "INSTAGRAM_APP_ID": "ig-app-id",
        "INSTAGRAM_APP_SECRET": "ig-app-secret",
        "INSTAGRAM_LOCKBOX_SECRET_ID": "ig-secret-id",
        "THREADS_APP_ID": "threads-app-id",
        "THREADS_APP_SECRET": "threads-app-secret",
        "THREADS_LOCKBOX_SECRET_ID": "threads-secret-id",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _event(path, *, method="GET", query=None, body=None):
    return {
        "path": path,
        "httpMethod": method,
        "queryStringParameters": query or {},
        "body": body or "",
    }


def _signed_request(payload, secret):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_signature}.{encoded}"


def test_health_is_public_and_contains_no_configuration():
    response = social_oauth.handler(_event("/health"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}


@pytest.mark.parametrize(
    ("platform", "app_id", "scope"),
    [
        (
            "instagram",
            "ig-app-id",
            "instagram_business_basic,instagram_business_content_publish",
        ),
        ("threads", "threads-app-id", "threads_basic,threads_content_publish"),
    ],
)
def test_start_redirects_with_minimal_scopes(platform, app_id, scope):
    response = social_oauth.handler(_event(f"/oauth/meta/{platform}/start"), None)

    assert response["statusCode"] == 302
    parsed = urlparse(response["headers"]["Location"])
    query = parse_qs(parsed.query)
    assert query["client_id"] == [app_id]
    assert query["scope"] == [scope]
    assert query["redirect_uri"] == [
        f"https://oauth.example.test/oauth/meta/{platform}/callback"
    ]
    assert query["force_reauth"] == ["true"]
    if platform == "instagram":
        assert query["enable_fb_login"] == ["false"]
    assert query["state"][0]
    assert response["headers"]["Cache-Control"] == "no-store"


def test_state_rejects_wrong_platform_and_expiry():
    state = social_oauth._state("instagram", "ig-app-secret", now=1_000)

    social_oauth._verify_state(state, "instagram", "ig-app-secret", now=1_100)
    with pytest.raises(social_oauth.OAuthFlowError, match="platform mismatch"):
        social_oauth._verify_state(state, "threads", "ig-app-secret", now=1_100)
    with pytest.raises(social_oauth.OAuthFlowError, match="expired"):
        social_oauth._verify_state(state, "instagram", "ig-app-secret", now=2_000)


def test_callback_stores_token_without_returning_it(monkeypatch):
    state = social_oauth._state("instagram", "ig-app-secret")
    stored = []
    monkeypatch.setattr(
        social_oauth,
        "_instagram_tokens",
        lambda code, config: {
            "access_token": "very-secret-token",
            "expires_in": 5_184_000,
            "permissions": "instagram_business_basic,instagram_business_content_publish",
            "user_id": "123",
        },
    )
    monkeypatch.setattr(
        social_oauth,
        "_profile",
        lambda platform, token: {"id": "123", "username": "cs2results"},
    )
    monkeypatch.setattr(
        social_oauth,
        "_store_credentials",
        lambda platform, config, token_data, profile, context: stored.append(
            (platform, token_data, profile)
        ),
    )

    response = social_oauth.handler(
        _event(
            "/oauth/meta/instagram/callback",
            query={"code": "one-time-code", "state": state},
        ),
        {"token": "iam-token"},
    )

    assert response["statusCode"] == 200
    assert "cs2results" in response["body"]
    assert "very-secret-token" not in response["body"]
    assert stored[0][0] == "instagram"


def test_callback_rejects_unsigned_state(monkeypatch):
    monkeypatch.setattr(
        social_oauth,
        "_instagram_tokens",
        lambda *args, **kwargs: pytest.fail("token exchange must not run"),
    )

    response = social_oauth.handler(
        _event(
            "/oauth/meta/instagram/callback",
            query={"code": "one-time-code", "state": "invalid"},
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert "invalid OAuth state" in response["body"]


def test_store_credentials_rejects_another_account(monkeypatch):
    monkeypatch.setattr(
        social_oauth.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Lockbox must not be called"),
    )
    config = social_oauth._platform_config("instagram")

    with pytest.raises(social_oauth.OAuthFlowError, match="not allowed"):
        social_oauth._store_credentials(
            "instagram",
            config,
            {"access_token": "secret", "expires_in": 60},
            {"id": "456", "username": "another_account"},
            {"token": "iam-token"},
        )


def test_store_credentials_adds_lockbox_version(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(social_oauth.requests, "post", fake_post)
    config = social_oauth._platform_config("threads")

    social_oauth._store_credentials(
        "threads",
        config,
        {
            "access_token": "threads-secret-token",
            "expires_in": 5_184_000,
            "permissions": "threads_basic,threads_content_publish",
        },
        {"id": "789", "username": "cs2results"},
        {"token": "iam-token"},
    )

    assert captured["url"].endswith("/threads-secret-id:addVersion")
    assert captured["headers"]["Authorization"] == "Bearer iam-token"
    entries = {entry["key"]: entry["textValue"] for entry in captured["json"]["payloadEntries"]}
    assert entries["ACCESS_TOKEN"] == "threads-secret-token"
    assert entries["USER_ID"] == "789"
    assert entries["USERNAME"] == "cs2results"


def test_data_deletion_requires_meta_signature(monkeypatch):
    removed = []
    monkeypatch.setattr(
        social_oauth,
        "_remove_credentials",
        lambda platform, config, context: removed.append(platform),
    )
    signed_request = _signed_request({"user_id": "123"}, "threads-app-secret")

    response = social_oauth.handler(
        _event(
            "/oauth/meta/threads/data-deletion",
            method="POST",
            body=f"signed_request={signed_request}",
        ),
        {"token": "iam-token"},
    )

    assert response["statusCode"] == 200
    assert removed == ["threads"]
    payload = json.loads(response["body"])
    assert payload["url"].startswith("https://oauth.example.test/")
    assert payload["confirmation_code"]
