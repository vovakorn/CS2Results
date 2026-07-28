from __future__ import annotations

from typing import Any

from ..models import SourceUnavailableError


async def read_limited_response(response: Any, limit: int, source_name: str) -> bytes:
    """Read a streamed response fully while enforcing a hard byte limit."""
    if response.content_length and response.content_length > limit:
        raise SourceUnavailableError(f"{source_name} response exceeds size limit")

    payload = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        payload.extend(chunk)
        if len(payload) > limit:
            raise SourceUnavailableError(f"{source_name} response exceeds size limit")
    return bytes(payload)
