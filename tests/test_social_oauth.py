import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import pytest

from cs2bot import social_oauth


@pytest.fixture(autouse=True)
def oauth_environment(monkeypatch):
    monkeypatch.delenv("SOCIAL_PROXY_URL", raising=False)
    monkeypatch.delenv("XRAY_CONFIG_JSON", raising=False)
    values = {
        "SOCIAL_OAUTH_BASE_URL": "https://oauth.example.test",
        "SOCIAL_OAUTH_EXPECTED_USERNAME": "cs2results",
        "INSTAGRAM_APP_ID": "ig-app-id",
        "INSTAGRAM_APP_SECRET": "ig-app-secret",
        "INSTAGRAM_LOCKBOX_SECRET_ID": "ig-secret-id",
        "INSTAGRAM_EXPECTED_USER_ID": "123",
        "THREADS_APP_ID": "threads-app-id",
        "THREADS_APP_SECRET": "threads-app-secret",
        "THREADS_LOCKBOX_SECRET_ID": "threads-secret-id",
        "THREADS_EXPECTED_USER_ID": "789",
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


def test_iam_token_reads_access_token_from_yandex_context():
    assert social_oauth._iam_token({"token": {"access_token": "iam-token"}}) == "iam-token"


def test_iam_token_falls_back_to_yandex_metadata_service(monkeypatch):
    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"access_token": "metadata-iam-token"}

    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(social_oauth.requests, "get", fake_get)

    assert social_oauth._iam_token(None) == "metadata-iam-token"
    assert captured["url"] == social_oauth.IAM_METADATA_URL
    assert captured["headers"] == {"Metadata-Flavor": "Google"}


def test_meta_requests_use_explicit_social_proxy(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv(
        "SOCIAL_PROXY_URL",
        "http://proxy-user:proxy-password@proxy.example:8443",
    )
    monkeypatch.setattr(social_oauth.requests, "request", fake_request)

    assert social_oauth._request_json("GET", "https://graph.instagram.com/me") == {
        "ok": True
    }
    assert captured["proxies"] == {
        "http": "http://proxy-user:proxy-password@proxy.example:8443",
        "https": "http://proxy-user:proxy-password@proxy.example:8443",
    }
    assert captured["headers"] == {"User-Agent": "curl/8.7.1"}


def test_social_proxy_rejects_non_http_url(monkeypatch):
    monkeypatch.setenv("SOCIAL_PROXY_URL", "socks5://proxy.example:1080")
    monkeypatch.setattr(
        social_oauth.requests,
        "request",
        lambda *args, **kwargs: pytest.fail("invalid proxy must fail before request"),
    )

    with pytest.raises(
        social_oauth.OAuthConfigurationError,
        match="absolute HTTP",
    ):
        social_oauth._request_json("GET", "https://graph.threads.net/me")


def test_meta_request_error_exposes_only_error_identifiers(monkeypatch):
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {
                "error": {
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 460,
                    "fbtrace_id": "safe-trace-id",
                    "message": "must not be exposed",
                }
            }

    monkeypatch.setattr(social_oauth.requests, "request", lambda *args, **kwargs: Response())

    with pytest.raises(
        social_oauth.OAuthFlowError,
        match=(
            r"HTTP 400 \(OAuthException, code=190, error_subcode=460, "
            r"fbtrace_id=safe-trace-id\)"
        ),
    ):
        social_oauth._request_json("GET", "https://graph.instagram.com/access_token")


def test_instagram_long_lived_exchange_reports_granted_scopes(monkeypatch):
    responses = iter(
        [
            {
                "access_token": "short-token",
                "permissions": "instagram_business_basic",
            },
            social_oauth.OAuthFlowError("Instagram long-lived-token exchange returned HTTP 400"),
        ]
    )

    def fake_request_json(*args, **kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(social_oauth, "_request_json", fake_request_json)

    with pytest.raises(
        social_oauth.OAuthFlowError,
        match=r"short-token scopes=instagram_business_basic",
    ):
        social_oauth._instagram_tokens("code", social_oauth._platform_config("instagram"))


def test_threads_authorization_code_exchange_uses_query_parameters(monkeypatch):
    calls = []

    def fake_request_json(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/oauth/access_token"):
            return {"access_token": "short-token", "user_id": "threads-user-id"}
        return {"access_token": "long-token", "expires_in": 5_184_000}

    monkeypatch.setattr(social_oauth, "_request_json", fake_request_json)

    result = social_oauth._threads_tokens("authorization-code", social_oauth._platform_config("threads"))

    assert result["access_token"] == "long-token"
    assert calls[0][0] == "POST"
    assert calls[0][2]["params"]["code"] == "authorization-code"
    assert calls[0][2]["params"]["grant_type"] == "authorization_code"
    assert "data" not in calls[0][2]


def test_threads_credentials_check_returns_only_boolean(monkeypatch):
    class XrayContext:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(social_oauth, "xray_http_proxy", lambda: XrayContext())
    monkeypatch.setattr(social_oauth, "_request_json", lambda *args, **kwargs: {"access_token": "app-token"})

    response = social_oauth.handler({"internal_job": "threads_credentials_check"}, None)

    assert response == {"statusCode": 200, "body": '{"ok": true}'}


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


def test_callback_uses_xray_proxy_when_configured(monkeypatch):
    state = social_oauth._state("instagram", "ig-app-secret")
    captured = []

    class XrayContext:
        def __enter__(self):
            return {"http": "http://127.0.0.1:18080", "https": "http://127.0.0.1:18080"}

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("XRAY_CONFIG_JSON", '{"outbounds": []}')
    monkeypatch.setattr(social_oauth, "xray_http_proxy", lambda: XrayContext())
    monkeypatch.setattr(social_oauth, "_store_credentials", lambda *args: None)

    def fake_tokens(code, config):
        captured.append(social_oauth._meta_proxy())
        return {"access_token": "token", "expires_in": 60}

    monkeypatch.setattr(social_oauth, "_instagram_tokens", fake_tokens)
    monkeypatch.setattr(social_oauth, "_profile", lambda *args: {"id": "123", "username": "cs2results"})

    response = social_oauth.handler(
        _event("/oauth/meta/instagram/callback", query={"code": "code", "state": state}),
        {"token": "iam-token"},
    )

    assert response["statusCode"] == 200
    assert captured == [{"http": "http://127.0.0.1:18080", "https": "http://127.0.0.1:18080"}]


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
    assert entries["APP_SECRET"] == "threads-app-secret"
    assert entries["ACCESS_TOKEN"] == "threads-secret-token"
    assert entries["USER_ID"] == "789"
    assert entries["USERNAME"] == "cs2results"


def test_lockbox_write_does_not_use_social_proxy(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("SOCIAL_PROXY_URL", "http://proxy.example:8443")
    monkeypatch.setattr(social_oauth.requests, "post", fake_post)

    social_oauth._store_credentials(
        "threads",
        social_oauth._platform_config("threads"),
        {"access_token": "token", "expires_in": 60},
        {"id": "789", "username": "cs2results"},
        {"token": "iam-token"},
    )

    assert "proxies" not in captured


def test_data_deletion_requires_meta_signature(monkeypatch):
    removed = []
    monkeypatch.setattr(
        social_oauth,
        "_remove_credentials",
        lambda platform, config, context: removed.append(platform),
    )
    signed_request = _signed_request({"user_id": "789"}, "threads-app-secret")

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


def test_data_deletion_rejects_a_different_signed_user(monkeypatch):
    monkeypatch.setattr(
        social_oauth,
        "_remove_credentials",
        lambda *args, **kwargs: pytest.fail("another user must not clear credentials"),
    )
    signed_request = _signed_request({"user_id": "other-user"}, "threads-app-secret")

    response = social_oauth.handler(
        _event(
            "/oauth/meta/threads/data-deletion",
            method="POST",
            body=f"signed_request={signed_request}",
        ),
        {"token": "iam-token"},
    )

    assert response["statusCode"] == 400
    assert "not allowed" in response["body"]
