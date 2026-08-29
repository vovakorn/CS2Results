"""OAuth callback handler for the project's owned Instagram and Threads accounts.

This module is deployed as a separate Yandex Cloud Function. It never returns
access tokens to the browser and writes successful OAuth results directly to a
dedicated Lockbox secret version.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger(__name__)

INSTAGRAM_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH_URL = "https://graph.instagram.com"
THREADS_AUTHORIZE_URL = os.getenv(
    "THREADS_AUTHORIZE_URL",
    "https://threads.net/oauth/authorize",
)
THREADS_GRAPH_URL = "https://graph.threads.net"
LOCKBOX_API_URL = "https://lockbox.api.cloud.yandex.net"

SUPPORTED_PLATFORMS = {"instagram", "threads"}
STATE_TTL_SECONDS = 10 * 60
HTTP_TIMEOUT_SECONDS = 15


class OAuthConfigurationError(RuntimeError):
    """Raised when the callback function is missing required configuration."""


class OAuthFlowError(RuntimeError):
    """Raised for a safe OAuth failure that must not contain credentials."""


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise OAuthConfigurationError(f"{name} is not configured")
    return value


def _platform_config(platform: str) -> dict[str, str]:
    if platform not in SUPPORTED_PLATFORMS:
        raise OAuthFlowError("unsupported platform")
    prefix = platform.upper()
    return {
        "app_id": _env(f"{prefix}_APP_ID"),
        "app_secret": _env(f"{prefix}_APP_SECRET"),
        "lockbox_secret_id": _env(f"{prefix}_LOCKBOX_SECRET_ID"),
        "expected_username": os.getenv(
            f"{prefix}_EXPECTED_USERNAME",
            os.getenv("SOCIAL_OAUTH_EXPECTED_USERNAME", "cs2results"),
        ).lstrip("@").casefold(),
        "expected_user_id": os.getenv(f"{prefix}_EXPECTED_USER_ID", "").strip(),
    }


def _base_url() -> str:
    return _env("SOCIAL_OAUTH_BASE_URL").rstrip("/")


def _callback_url(platform: str) -> str:
    return f"{_base_url()}/oauth/meta/{platform}/callback"


def _social_proxy() -> dict[str, str] | None:
    """Return the explicit Meta egress proxy without exposing it in logs."""
    value = os.getenv("SOCIAL_PROXY_URL", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OAuthConfigurationError(
            "SOCIAL_PROXY_URL must be an absolute HTTP(S) URL"
        )
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise OAuthConfigurationError(
            "SOCIAL_PROXY_URL must not contain a path, query, or fragment"
        )
    return {"http": value, "https": value}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _state(platform: str, app_secret: str, now: int | None = None) -> str:
    payload = {
        "platform": platform,
        "issued_at": int(time.time() if now is None else now),
        "nonce": secrets.token_urlsafe(16),
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(app_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{encoded}.{_b64encode(signature.digest())}"


def _verify_state(
    value: str,
    platform: str,
    app_secret: str,
    now: int | None = None,
) -> None:
    try:
        encoded, supplied_signature = value.split(".", 1)
        expected_signature = hmac.new(
            app_secret.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            raise OAuthFlowError("invalid OAuth state")
        payload = json.loads(_b64decode(encoded))
        issued_at = int(payload["issued_at"])
    except OAuthFlowError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise OAuthFlowError("invalid OAuth state") from exc

    reference = int(time.time() if now is None else now)
    if payload.get("platform") != platform:
        raise OAuthFlowError("OAuth state platform mismatch")
    if issued_at > reference + 30 or reference - issued_at > STATE_TTL_SECONDS:
        raise OAuthFlowError("OAuth state expired")


def _signed_request_payload(value: str, app_secret: str) -> dict[str, Any]:
    try:
        encoded_signature, encoded_payload = value.split(".", 1)
        expected_signature = hmac.new(
            app_secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(encoded_signature), expected_signature):
            raise OAuthFlowError("invalid signed request")
        payload = json.loads(_b64decode(encoded_payload))
    except OAuthFlowError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OAuthFlowError("invalid signed request") from exc
    if not isinstance(payload, dict) or not payload.get("user_id"):
        raise OAuthFlowError("invalid signed request")
    return payload


def _query(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("queryStringParameters") or event.get("query") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def _path(event: dict[str, Any]) -> str:
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") if isinstance(request_context, dict) else {}
    candidates = (
        event.get("path"),
        event.get("url"),
        http_context.get("path") if isinstance(http_context, dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate.split("?", 1)[0]
    return "/"


def _method(event: dict[str, Any]) -> str:
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") if isinstance(request_context, dict) else {}
    method = event.get("httpMethod")
    if not method and isinstance(http_context, dict):
        method = http_context.get("method")
    return str(method or "GET").upper()


def _form(event: dict[str, Any]) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = _b64decode(str(body)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise OAuthFlowError("invalid request body") from exc
    parsed = parse_qs(str(body), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _request_json(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            proxies=_social_proxy(),
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise OAuthFlowError("remote OAuth request failed") from exc
    if response.status_code >= 300:
        raise OAuthFlowError(f"remote OAuth endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise OAuthFlowError("remote OAuth endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthFlowError("remote OAuth endpoint returned invalid data")
    return payload


def _instagram_tokens(code: str, config: dict[str, str]) -> dict[str, Any]:
    short_payload = _request_json(
        "POST",
        INSTAGRAM_TOKEN_URL,
        data={
            "client_id": config["app_id"],
            "client_secret": config["app_secret"],
            "grant_type": "authorization_code",
            "redirect_uri": _callback_url("instagram"),
            "code": code,
        },
    )
    candidate: dict[str, Any] = short_payload
    data = short_payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        candidate = data[0]
    short_token = candidate.get("access_token")
    if not isinstance(short_token, str) or not short_token:
        raise OAuthFlowError("Instagram did not return an access token")

    long_payload = _request_json(
        "GET",
        f"{INSTAGRAM_GRAPH_URL}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": config["app_secret"],
            "access_token": short_token,
        },
    )
    return {
        "access_token": long_payload.get("access_token"),
        "expires_in": long_payload.get("expires_in"),
        "permissions": candidate.get("permissions", ""),
        "user_id": candidate.get("user_id"),
    }


def _threads_tokens(code: str, config: dict[str, str]) -> dict[str, Any]:
    short_payload = _request_json(
        "POST",
        f"{THREADS_GRAPH_URL}/oauth/access_token",
        params={
            "client_id": config["app_id"],
            "client_secret": config["app_secret"],
            "grant_type": "authorization_code",
            "redirect_uri": _callback_url("threads"),
            "code": code,
        },
    )
    short_token = short_payload.get("access_token")
    if not isinstance(short_token, str) or not short_token:
        raise OAuthFlowError("Threads did not return an access token")
    long_payload = _request_json(
        "GET",
        f"{THREADS_GRAPH_URL}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": config["app_secret"],
            "access_token": short_token,
        },
    )
    return {
        "access_token": long_payload.get("access_token"),
        "expires_in": long_payload.get("expires_in"),
        "permissions": "threads_basic,threads_content_publish",
        "user_id": short_payload.get("user_id"),
    }


def _profile(platform: str, access_token: str) -> dict[str, Any]:
    base_url = INSTAGRAM_GRAPH_URL if platform == "instagram" else THREADS_GRAPH_URL
    return _request_json(
        "GET",
        f"{base_url}/me",
        params={"fields": "id,username"},
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _iam_token(context: Any) -> str:
    if isinstance(context, dict):
        token = context.get("token")
    else:
        token = getattr(context, "token", None)
    if not isinstance(token, str) or not token:
        raise OAuthConfigurationError("function service account IAM token is unavailable")
    return token


def _store_credentials(
    platform: str,
    config: dict[str, str],
    token_data: dict[str, Any],
    profile: dict[str, Any],
    context: Any,
) -> None:
    access_token = token_data.get("access_token")
    user_id = profile.get("id") or token_data.get("user_id")
    username = profile.get("username")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthFlowError(f"{platform} long-lived access token is missing")
    if not isinstance(user_id, (str, int)) or not str(user_id):
        raise OAuthFlowError(f"{platform} user id is missing")
    if not isinstance(username, str) or username.lstrip("@").casefold() != config["expected_username"]:
        raise OAuthFlowError(f"authorized {platform} account is not allowed")
    if config["expected_user_id"] and str(user_id) != config["expected_user_id"]:
        raise OAuthFlowError(f"authorized {platform} account id is not allowed")

    try:
        expires_in = max(0, int(token_data.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    payload_entries = [
        {"key": "ACCESS_TOKEN", "textValue": access_token},
        {"key": "USER_ID", "textValue": str(user_id)},
        {"key": "USERNAME", "textValue": username.lstrip("@")},
        {"key": "TOKEN_EXPIRES_AT", "textValue": expires_at.isoformat().replace("+00:00", "Z")},
        {"key": "GRANTED_SCOPES", "textValue": str(token_data.get("permissions") or "")},
    ]
    response = requests.post(
        (
            f"{LOCKBOX_API_URL}/lockbox/v1/secrets/"
            f"{config['lockbox_secret_id']}:addVersion"
        ),
        headers={"Authorization": f"Bearer {_iam_token(context)}"},
        json={
            "description": f"{platform} OAuth authorization for @{username.lstrip('@')}",
            "payloadEntries": payload_entries,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if response.status_code >= 300:
        raise OAuthFlowError(f"Lockbox returned HTTP {response.status_code}")


def _remove_credentials(platform: str, config: dict[str, str], context: Any) -> None:
    response = requests.post(
        (
            f"{LOCKBOX_API_URL}/lockbox/v1/secrets/"
            f"{config['lockbox_secret_id']}:addVersion"
        ),
        headers={"Authorization": f"Bearer {_iam_token(context)}"},
        json={
            "description": f"{platform} authorization removed",
            "payloadEntries": [
                {"key": "ACCESS_TOKEN"},
                {"key": "USER_ID"},
                {"key": "USERNAME"},
                {"key": "TOKEN_EXPIRES_AT"},
                {"key": "GRANTED_SCOPES"},
            ],
        },
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if response.status_code >= 300:
        raise OAuthFlowError(f"Lockbox returned HTTP {response.status_code}")


def _redirect(location: str) -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {
            "Location": location,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
        "body": "",
    }


def _html_response(status_code: int, title: str, message: str) -> dict[str, Any]:
    body = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<body style='font-family:system-ui;max-width:680px;margin:64px auto;padding:0 20px'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
        "body": body,
    }


def _platform_and_action(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4 or parts[:2] != ["oauth", "meta"]:
        raise OAuthFlowError("unknown endpoint")
    platform, action = parts[2], parts[3]
    if platform not in SUPPORTED_PLATFORMS:
        raise OAuthFlowError("unsupported platform")
    return platform, action


def _start(platform: str) -> dict[str, Any]:
    config = _platform_config(platform)
    if platform == "instagram":
        authorize_url = INSTAGRAM_AUTHORIZE_URL
        scope = "instagram_business_basic,instagram_business_content_publish"
    else:
        authorize_url = THREADS_AUTHORIZE_URL
        scope = "threads_basic,threads_content_publish"
    params = {
        "client_id": config["app_id"],
        "redirect_uri": _callback_url(platform),
        "response_type": "code",
        "scope": scope,
        "state": _state(platform, config["app_secret"]),
        "force_reauth": "true",
    }
    if platform == "instagram":
        # Do not let Accounts Center silently reuse the app owner's Facebook
        # session. The owned professional Instagram account must authenticate
        # with its own Instagram credentials.
        params["enable_fb_login"] = "false"
    return _redirect(f"{authorize_url}?{urlencode(params)}")


def _callback(platform: str, query: dict[str, str], context: Any) -> dict[str, Any]:
    if query.get("error"):
        raise OAuthFlowError("authorization was cancelled")
    code = query.get("code", "").removesuffix("#_")
    state = query.get("state", "")
    if not code or not state:
        raise OAuthFlowError("authorization code or state is missing")
    config = _platform_config(platform)
    _verify_state(state, platform, config["app_secret"])
    token_data = (
        _instagram_tokens(code, config)
        if platform == "instagram"
        else _threads_tokens(code, config)
    )
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthFlowError("long-lived access token is missing")
    profile = _profile(platform, access_token)
    _store_credentials(platform, config, token_data, profile, context)
    return _html_response(
        200,
        "Авторизация завершена",
        f"Аккаунт @{profile.get('username')} подключён к {platform.title()}. Можно закрыть окно.",
    )


def _signed_request(event: dict[str, Any], platform: str) -> dict[str, Any]:
    config = _platform_config(platform)
    value = _form(event).get("signed_request", "")
    if not value:
        raise OAuthFlowError("signed_request is missing")
    payload = _signed_request_payload(value, config["app_secret"])
    expected_user_id = config["expected_user_id"]
    if not expected_user_id or str(payload["user_id"]) != expected_user_id:
        raise OAuthFlowError("signed request account is not allowed")
    return payload


def _deauthorize(event: dict[str, Any], platform: str, context: Any) -> dict[str, Any]:
    _signed_request(event, platform)
    config = _platform_config(platform)
    _remove_credentials(platform, config, context)
    return {"statusCode": 200, "headers": {"Cache-Control": "no-store"}, "body": "{}"}


def _data_deletion(event: dict[str, Any], platform: str, context: Any) -> dict[str, Any]:
    payload = _signed_request(event, platform)
    config = _platform_config(platform)
    _remove_credentials(platform, config, context)
    confirmation_code = _b64encode(
        hmac.new(
            config["app_secret"].encode("utf-8"),
            f"{platform}:{payload['user_id']}".encode("utf-8"),
            hashlib.sha256,
        ).digest()[:18]
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(
            {
                "url": (
                    f"{_base_url()}/oauth/meta/{platform}/deletion-status"
                    f"?code={confirmation_code}"
                ),
                "confirmation_code": confirmation_code,
            }
        ),
    }


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Handle API Gateway OAuth routes without exposing credentials."""
    event = event if isinstance(event, dict) else {}
    path = _path(event)
    if path == "/health":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps({"ok": True}),
        }
    try:
        platform, action = _platform_and_action(path)
        method = _method(event)
        if action == "start" and method == "GET":
            return _start(platform)
        if action == "callback" and method == "GET":
            return _callback(platform, _query(event), context)
        if action == "deauthorize" and method == "POST":
            return _deauthorize(event, platform, context)
        if action == "data-deletion" and method == "POST":
            return _data_deletion(event, platform, context)
        if action == "deletion-status" and method == "GET":
            return _html_response(200, "Данные удалены", "Авторизация и сохранённые токены удалены.")
        return _html_response(404, "Не найдено", "Неизвестный OAuth endpoint.")
    except OAuthConfigurationError as exc:
        logger.error("event=social_oauth_configuration_error error=%s", str(exc))
        return _html_response(503, "Сервис ещё не настроен", "Завершите настройку секретов в Lockbox.")
    except OAuthFlowError as exc:
        logger.warning("event=social_oauth_failed reason=%s", str(exc))
        return _html_response(400, "Авторизация не завершена", str(exc))
    except Exception as exc:
        logger.exception("event=social_oauth_unexpected error_type=%s", type(exc).__name__)
        return _html_response(502, "Ошибка авторизации", "Повторите попытку позже.")
