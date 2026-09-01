"""Instagram delivery primitives shared by scheduled social publishing jobs.

The module deliberately has no handler.  A scheduler owns content selection and
deduplication; this code only uploads already-rendered cards and asks Instagram
to publish them.  Public media is kept in a separate bucket from operational
delivery state so publishing a card never exposes claims, outbox records, or
source caches.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence
from urllib.parse import quote

import boto3
import requests

from .xray_proxy import XrayProxyError, xray_http_proxy


INSTAGRAM_GRAPH_URL = "https://graph.instagram.com"
LOCKBOX_PAYLOAD_URL = "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets"
IAM_METADATA_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
HTTP_TIMEOUT_SECONDS = 20
META_HTTP_HEADERS = {"User-Agent": "curl/8.7.1"}
MAX_CAROUSEL_ITEMS = 10


class InstagramPublishError(RuntimeError):
    """Safe failure message suitable for logs and user-facing diagnostics."""


class InstagramDeliveryUncertainError(InstagramPublishError):
    """Instagram may have accepted the request, so automatic retry is unsafe."""


def instagram_publishing_enabled() -> bool:
    """Keep scheduled publishing opt-in until its media bucket is provisioned."""
    value = os.getenv("ENABLE_INSTAGRAM_PUBLISHING", "0").strip().casefold()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n", ""}:
        return False
    raise InstagramPublishError("ENABLE_INSTAGRAM_PUBLISHING must be a boolean")


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise InstagramPublishError(f"{name} is not configured")
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
        raise InstagramPublishError("function service account IAM token is unavailable") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise InstagramPublishError("function service account IAM token is unavailable")
    return token


def _instagram_credentials(context: Any) -> tuple[str, str]:
    secret_id = _env("INSTAGRAM_LOCKBOX_SECRET_ID")
    try:
        response = requests.get(
            f"{LOCKBOX_PAYLOAD_URL}/{secret_id}/payload",
            headers={"Authorization": f"Bearer {_iam_token(context)}"},
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise InstagramPublishError("Instagram OAuth credentials are unavailable") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    values = {
        item.get("key"): item.get("textValue")
        for item in entries or []
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and isinstance(item.get("textValue"), str)
    }
    access_token, user_id = values.get("ACCESS_TOKEN"), values.get("USER_ID")
    if not access_token or not user_id:
        raise InstagramPublishError("Instagram OAuth credentials are unavailable")
    return access_token, user_id


def _media_bucket() -> str:
    return _env("INSTAGRAM_MEDIA_BUCKET")


def _media_base_url(bucket: str) -> str:
    configured = os.getenv("INSTAGRAM_MEDIA_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return configured or f"https://storage.yandexcloud.net/{quote(bucket, safe='.-_') }"


def _media_client() -> Any:
    endpoint = os.getenv("OBJECT_STORAGE_ENDPOINT", "https://storage.yandexcloud.net")
    return boto3.client("s3", endpoint_url=endpoint)


def upload_public_cards(publication_key: str, cards: Sequence[bytes]) -> list[str]:
    """Upload square PNG cards under a non-secret deterministic publication key."""
    if not cards or len(cards) > MAX_CAROUSEL_ITEMS:
        raise InstagramPublishError("Instagram publication must contain between one and ten cards")
    if not publication_key or any(part not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for part in publication_key):
        raise InstagramPublishError("Instagram publication key is invalid")
    bucket = _media_bucket()
    base_url = _media_base_url(bucket)
    client = _media_client()
    urls: list[str] = []
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, bytes) or not card:
            raise InstagramPublishError("Instagram card is invalid")
        key = f"instagram/{publication_key}/{index}.png"
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=card,
                ContentType="image/png",
                ACL="public-read",
                CacheControl="public, max-age=31536000, immutable",
            )
        except Exception as exc:
            raise InstagramPublishError("Instagram media upload failed") from exc
        urls.append(f"{base_url}/{quote(key, safe='/')}")
    return urls


def _meta_post(url: str, data: dict[str, str], proxy: dict[str, str] | None) -> dict[str, Any]:
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
        raise InstagramDeliveryUncertainError("Instagram publish request outcome is unknown") from exc
    if response.status_code >= 300:
        raise InstagramPublishError(f"Instagram publish endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise InstagramPublishError("Instagram publish endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise InstagramPublishError("Instagram publish endpoint returned invalid data")
    return payload


def _container_id(payload: dict[str, Any]) -> str:
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise InstagramPublishError("Instagram did not create a media container")
    return identifier


def _caption(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InstagramPublishError("Instagram caption is empty")
    if len(normalized) > 2200:
        raise InstagramPublishError("Instagram caption exceeds 2200 characters")
    return normalized


@contextmanager
def _meta_proxy() -> Iterator[dict[str, str] | None]:
    try:
        with xray_http_proxy() as proxy:
            yield proxy
    except XrayProxyError as exc:
        raise InstagramPublishError("Xray client is unavailable") from exc


def publish_cards(image_urls: Sequence[str], caption: str, context: Any) -> str:
    """Create and publish one photo or a carousel. Returns only Meta media ID."""
    if not image_urls or len(image_urls) > MAX_CAROUSEL_ITEMS:
        raise InstagramPublishError("Instagram publication must contain between one and ten cards")
    if not all(isinstance(url, str) and url.startswith("https://") for url in image_urls):
        raise InstagramPublishError("Instagram image URL is invalid")
    access_token, user_id = _instagram_credentials(context)
    with _meta_proxy() as proxy:
        if len(image_urls) == 1:
            container = _meta_post(
                f"{INSTAGRAM_GRAPH_URL}/{user_id}/media",
                {"image_url": image_urls[0], "caption": _caption(caption), "access_token": access_token},
                proxy,
            )
            creation_id = _container_id(container)
        else:
            child_ids = []
            for image_url in image_urls:
                child = _meta_post(
                    f"{INSTAGRAM_GRAPH_URL}/{user_id}/media",
                    {"image_url": image_url, "is_carousel_item": "true", "access_token": access_token},
                    proxy,
                )
                child_ids.append(_container_id(child))
            parent = _meta_post(
                f"{INSTAGRAM_GRAPH_URL}/{user_id}/media",
                {
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "caption": _caption(caption),
                    "access_token": access_token,
                },
                proxy,
            )
            creation_id = _container_id(parent)
        published = _meta_post(
            f"{INSTAGRAM_GRAPH_URL}/{user_id}/media_publish",
            {"creation_id": creation_id, "access_token": access_token},
            proxy,
        )
    return _container_id(published)


def publish_rendered_cards(
    publication_key: str,
    cards: Sequence[bytes],
    caption: str,
    context: Any,
) -> str:
    """Upload cards first, then publish; upload retries are safe and idempotent."""
    return publish_cards(upload_public_cards(publication_key, cards), caption, context)
