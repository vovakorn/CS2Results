"""Private one-off social publishing handlers.

This module is intentionally deployed without an API Gateway route. It is invoked
only with authenticated Yandex Cloud CLI calls while social publishing is being
validated.
"""
from __future__ import annotations

import json
import os
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageDraw

from cs2bot.threads_publish import ThreadsPublishError, publish_rendered_cards as publish_threads_rendered_cards
from cs2bot.xray_proxy import XrayProxyError, xray_http_proxy

LOCKBOX_PAYLOAD_URL = "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets"
IAM_METADATA_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
INSTAGRAM_GRAPH_URL = "https://graph.instagram.com"
THREADS_GRAPH_URL = "https://graph.threads.net/v1.0"
HTTP_TIMEOUT_SECONDS = 20
META_HTTP_HEADERS = {"User-Agent": "curl/8.7.1"}
TEST_IMAGE_URL = "https://placehold.co/1080x1080/111827/FFFFFF.png?text=CS2Results%0AInstagram+test"
TEST_CAPTION = "Тест интеграции CS2Results с Instagram. ✅"
THREADS_TEST_TEXT = "Тест интеграции CS2Results с Threads. ✅"
THREADS_TEST_CARD_CAPTION = "Тестовая карточка CS2Results для Threads. ✅"
THREADS_TEST_CARD_KEY = "manual_threads_test_v1"


class SocialPublishError(RuntimeError):
    """Safe error that never includes an access token or a secret."""


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SocialPublishError(f"{name} is not configured")
    return value


def _iam_token(context: Any) -> str:
    token = context.get("token") if isinstance(context, dict) else getattr(context, "token", None)
    if isinstance(token, str) and token:
        return token
    if isinstance(token, dict):
        access_token = token.get("access_token")
        if isinstance(access_token, str) and access_token:
            return access_token
    try:
        response = requests.get(
            IAM_METADATA_URL,
            headers={"Metadata-Flavor": "Google"},
            timeout=2,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise SocialPublishError("function service account IAM token is unavailable") from exc
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise SocialPublishError("function service account IAM token is unavailable")
    return access_token


def _payload_entries(secret_id: str, iam_token: str) -> dict[str, str]:
    try:
        response = requests.get(
            f"{LOCKBOX_PAYLOAD_URL}/{secret_id}/payload",
            headers={"Authorization": f"Bearer {iam_token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise SocialPublishError("social OAuth credentials are unavailable") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise SocialPublishError("social OAuth credentials are unavailable")
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key, value = entry.get("key"), entry.get("textValue")
        if isinstance(key, str) and isinstance(value, str) and value:
            result[key] = value
    return result


def _instagram_credentials(context: Any) -> tuple[str, str]:
    entries = _payload_entries(_env("INSTAGRAM_LOCKBOX_SECRET_ID"), _iam_token(context))
    access_token, user_id = entries.get("ACCESS_TOKEN"), entries.get("USER_ID")
    if not access_token or not user_id:
        raise SocialPublishError("Instagram OAuth credentials are unavailable")
    return access_token, user_id


def _threads_credentials(context: Any) -> tuple[str, str]:
    entries = _payload_entries(_env("THREADS_LOCKBOX_SECRET_ID"), _iam_token(context))
    access_token, user_id = entries.get("ACCESS_TOKEN"), entries.get("USER_ID")
    if not access_token or not user_id:
        raise SocialPublishError("Threads OAuth credentials are unavailable")
    return access_token, user_id


def _meta_post(url: str, *, data: dict[str, str], proxy: dict[str, str] | None) -> dict[str, Any]:
    try:
        response = requests.post(
            url,
            data=data,
            headers=META_HTTP_HEADERS,
            proxies=proxy,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SocialPublishError("Meta publish request failed") from exc
    if response.status_code >= 300:
        raise SocialPublishError(f"Meta publish endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise SocialPublishError("Meta publish endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SocialPublishError("Meta publish endpoint returned invalid data")
    return payload


def publish_instagram_test_post(context: Any) -> dict[str, str]:
    access_token, user_id = _instagram_credentials(context)
    try:
        with xray_http_proxy() as proxy:
            container = _meta_post(
                f"{INSTAGRAM_GRAPH_URL}/{user_id}/media",
                data={
                    "image_url": os.getenv("SOCIAL_TEST_IMAGE_URL", TEST_IMAGE_URL),
                    "caption": os.getenv("SOCIAL_TEST_CAPTION", TEST_CAPTION),
                    "access_token": access_token,
                },
                proxy=proxy,
            )
            creation_id = container.get("id")
            if not isinstance(creation_id, str) or not creation_id:
                raise SocialPublishError("Instagram did not create a media container")
            published = _meta_post(
                f"{INSTAGRAM_GRAPH_URL}/{user_id}/media_publish",
                data={"creation_id": creation_id, "access_token": access_token},
                proxy=proxy,
            )
    except XrayProxyError as exc:
        raise SocialPublishError("Xray client is unavailable") from exc
    media_id = published.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise SocialPublishError("Instagram did not confirm publication")
    return {"media_id": media_id}


def publish_threads_test_post(context: Any) -> dict[str, str]:
    """Publish one explicit, text-only Threads connectivity test."""
    access_token, user_id = _threads_credentials(context)
    try:
        with xray_http_proxy() as proxy:
            container = _meta_post(
                f"{THREADS_GRAPH_URL}/{user_id}/threads",
                data={
                    "media_type": "TEXT",
                    "text": os.getenv("THREADS_TEST_TEXT", THREADS_TEST_TEXT),
                    "access_token": access_token,
                },
                proxy=proxy,
            )
            creation_id = container.get("id")
            if not isinstance(creation_id, str) or not creation_id:
                raise SocialPublishError("Threads did not create a media container")
            published = _meta_post(
                f"{THREADS_GRAPH_URL}/{user_id}/threads_publish",
                data={"creation_id": creation_id, "access_token": access_token},
                proxy=proxy,
            )
    except XrayProxyError as exc:
        raise SocialPublishError("Xray client is unavailable") from exc
    post_id = published.get("id")
    if not isinstance(post_id, str) or not post_id:
        raise SocialPublishError("Threads did not confirm publication")
    return {"post_id": post_id}


def _threads_test_card() -> bytes:
    """Render a deterministic, non-match card for an explicitly approved test."""
    image = Image.new("RGB", (1080, 1080), "#111827")
    draw = ImageDraw.Draw(image)
    draw.rectangle((64, 64, 1016, 1016), outline="#22d3ee", width=8)
    draw.rectangle((108, 420, 972, 660), fill="#172554", outline="#fbbf24", width=4)
    draw.text((394, 470), "CS2RESULTS", fill="#ffffff")
    draw.text((414, 550), "THREADS TEST", fill="#fbbf24")
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def publish_threads_test_card(context: Any) -> dict[str, str]:
    """Publish one explicit image-card test using the production Threads primitive."""
    try:
        post_id = publish_threads_rendered_cards(
            THREADS_TEST_CARD_KEY,
            [_threads_test_card()],
            os.getenv("THREADS_TEST_CARD_CAPTION", THREADS_TEST_CARD_CAPTION),
            context,
        )
    except ThreadsPublishError as exc:
        raise SocialPublishError(str(exc)) from exc
    return {"post_id": post_id}


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    job = event.get("job")
    if job not in {"instagram_test_post", "threads_test_post", "threads_test_card"}:
        return {"statusCode": 404, "body": json.dumps({"error": "unknown job"})}
    try:
        if job == "instagram_test_post":
            published = publish_instagram_test_post(context)
        elif job == "threads_test_post":
            published = publish_threads_test_post(context)
        else:
            published = publish_threads_test_card(context)
    except SocialPublishError as exc:
        return {"statusCode": 502, "body": json.dumps({"error": str(exc)})}
    return {"statusCode": 200, "body": json.dumps({"ok": True, **published})}
