"""Threads delivery primitives for already-rendered publication cards.

The scheduler owns content selection, the durable outbox and claims.  This
module only stores immutable public PNGs and performs the Threads two-step
container/publish workflow.  It deliberately has no HTTP handler and never
uses the Telegram proxy.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence
from urllib.parse import quote

import boto3
import requests

from .instagram_publish import LOCKBOX_PAYLOAD_URL, _iam_token
from .xray_proxy import XrayProxyError, xray_http_proxy


THREADS_GRAPH_URL = "https://graph.threads.net/v1.0"
HTTP_TIMEOUT_SECONDS = 20
META_HTTP_HEADERS = {"User-Agent": "curl/8.7.1"}
MAX_CAROUSEL_ITEMS = 20
MAX_TEXT_LENGTH = 500


class ThreadsPublishError(RuntimeError):
    """Safe failure message suitable for logs and user-facing diagnostics."""


class ThreadsDeliveryUncertainError(ThreadsPublishError):
    """Threads may have accepted a request, so automatic retry is unsafe."""


def threads_publishing_enabled() -> bool:
    """Keep scheduled Threads delivery opt-in until media storage is configured."""
    value = os.getenv("ENABLE_THREADS_PUBLISHING", "0").strip().casefold()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n", ""}:
        return False
    raise ThreadsPublishError("ENABLE_THREADS_PUBLISHING must be a boolean")


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ThreadsPublishError(f"{name} is not configured")
    return value


def _threads_credentials(context: Any) -> tuple[str, str]:
    secret_id = _env("THREADS_LOCKBOX_SECRET_ID")
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
        raise ThreadsPublishError("Threads OAuth credentials are unavailable") from exc
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
        raise ThreadsPublishError("Threads OAuth credentials are unavailable")
    return access_token, user_id


def _media_bucket() -> str:
    return _env("THREADS_MEDIA_BUCKET")


def _media_base_url(bucket: str) -> str:
    configured = os.getenv("THREADS_MEDIA_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return configured or f"https://storage.yandexcloud.net/{quote(bucket, safe='.-_')}"


def _media_client() -> Any:
    endpoint = os.getenv("OBJECT_STORAGE_ENDPOINT", "https://storage.yandexcloud.net")
    return boto3.client("s3", endpoint_url=endpoint)


def upload_public_cards(publication_key: str, cards: Sequence[bytes]) -> list[str]:
    """Persist public card URLs under a deterministic, immutable publication key."""
    if not cards or len(cards) > MAX_CAROUSEL_ITEMS:
        raise ThreadsPublishError("Threads publication must contain between one and twenty cards")
    if not publication_key or any(
        part not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for part in publication_key
    ):
        raise ThreadsPublishError("Threads publication key is invalid")
    bucket = _media_bucket()
    base_url = _media_base_url(bucket)
    client = _media_client()
    urls: list[str] = []
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, bytes) or not card:
            raise ThreadsPublishError("Threads card is invalid")
        key = f"threads/{publication_key}/{index}.png"
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
            raise ThreadsPublishError("Threads media upload failed") from exc
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
        raise ThreadsDeliveryUncertainError("Threads publish request outcome is unknown") from exc
    if response.status_code >= 500:
        raise ThreadsDeliveryUncertainError("Threads publish request outcome is unknown")
    if response.status_code >= 300:
        raise ThreadsPublishError(f"Threads publish endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise ThreadsDeliveryUncertainError("Threads publish request outcome is unknown") from exc
    if not isinstance(payload, dict):
        raise ThreadsDeliveryUncertainError("Threads publish request outcome is unknown")
    return payload


def _container_id(payload: dict[str, Any]) -> str:
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ThreadsPublishError("Threads did not create a media container")
    return identifier


def _published_post_id(payload: dict[str, Any]) -> str:
    """A malformed publish acknowledgement cannot prove that no post was made."""
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ThreadsDeliveryUncertainError("Threads publish request outcome is unknown")
    return identifier


def _caption(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ThreadsPublishError("Threads text is empty")
    if len(normalized) > MAX_TEXT_LENGTH:
        return normalized[: MAX_TEXT_LENGTH - 1].rstrip() + "…"
    return normalized


@contextmanager
def _meta_proxy() -> Iterator[dict[str, str] | None]:
    try:
        with xray_http_proxy() as proxy:
            yield proxy
    except XrayProxyError as exc:
        raise ThreadsPublishError("Xray client is unavailable") from exc


def publish_cards(image_urls: Sequence[str], caption: str, context: Any) -> str:
    """Create and publish one image or a Threads carousel; return only post ID."""
    if not image_urls or len(image_urls) > MAX_CAROUSEL_ITEMS:
        raise ThreadsPublishError("Threads publication must contain between one and twenty cards")
    if not all(isinstance(url, str) and url.startswith("https://") for url in image_urls):
        raise ThreadsPublishError("Threads image URL is invalid")
    access_token, user_id = _threads_credentials(context)
    with _meta_proxy() as proxy:
        if len(image_urls) == 1:
            container = _meta_post(
                f"{THREADS_GRAPH_URL}/{user_id}/threads",
                {
                    "media_type": "IMAGE",
                    "image_url": image_urls[0],
                    "text": _caption(caption),
                    "access_token": access_token,
                },
                proxy,
            )
            creation_id = _container_id(container)
        else:
            child_ids = []
            for image_url in image_urls:
                child = _meta_post(
                    f"{THREADS_GRAPH_URL}/{user_id}/threads",
                    {
                        "media_type": "IMAGE",
                        "image_url": image_url,
                        "is_carousel_item": "true",
                        "access_token": access_token,
                    },
                    proxy,
                )
                child_ids.append(_container_id(child))
            parent = _meta_post(
                f"{THREADS_GRAPH_URL}/{user_id}/threads",
                {
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "text": _caption(caption),
                    "access_token": access_token,
                },
                proxy,
            )
            creation_id = _container_id(parent)
        published = _meta_post(
            f"{THREADS_GRAPH_URL}/{user_id}/threads_publish",
            {"creation_id": creation_id, "access_token": access_token},
            proxy,
        )
    return _published_post_id(published)


def publish_rendered_cards(
    publication_key: str,
    cards: Sequence[bytes],
    caption: str,
    context: Any,
) -> str:
    """Upload cards before Meta work; the resulting URLs remain stable for the claim."""
    return publish_cards(upload_public_cards(publication_key, cards), caption, context)
