"""Store already-rendered public PNGs for social network posts."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote


IMAGE_LIMITS = {"x": 4, "threads": 20}
_SAFE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


class PublicMediaUploadError(RuntimeError):
    """A validated public-media upload could not be completed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_public_pngs(
    platform: str,
    publication_key: str,
    cards: Sequence[bytes],
) -> None:
    """Validate the deterministic key and complete album before uploading."""
    limit = IMAGE_LIMITS.get(platform)
    if limit is None:
        raise PublicMediaUploadError("unsupported_platform")
    if not cards or len(cards) > limit:
        raise PublicMediaUploadError("invalid_count")
    if not publication_key or any(char not in _SAFE_KEY_CHARS for char in publication_key):
        raise PublicMediaUploadError("invalid_key")
    if any(not isinstance(card, bytes) or not card for card in cards):
        raise PublicMediaUploadError("invalid_image")


def upload_public_pngs(
    platform: str,
    publication_key: str,
    cards: Sequence[bytes],
    *,
    bucket: str,
    base_url: str,
    client: Any,
) -> list[str]:
    """Upload one complete album and return stable public URLs.

    X uses at most four images under ``x/<publication_key>/``. Threads keeps
    its existing twenty-image limit and ``threads/<publication_key>/`` keys.
    """
    validate_public_pngs(platform, publication_key, cards)
    urls: list[str] = []
    for index, card in enumerate(cards, start=1):
        key = f"{platform}/{publication_key}/{index}.png"
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
            raise PublicMediaUploadError("upload_failed") from exc
        urls.append(f"{base_url.rstrip('/')}/{quote(key, safe='/')}")
    return urls
