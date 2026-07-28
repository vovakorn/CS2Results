import asyncio

import pytest

from cs2bot.match_sources.models import SourceUnavailableError
from cs2bot.match_sources.sources.http_utils import read_limited_response


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, chunks, content_length=None):
        self.content = FakeContent(chunks)
        self.content_length = content_length


def test_read_limited_response_reads_all_stream_chunks():
    response = FakeResponse([b'{"data":', b"[]}"])

    assert asyncio.run(read_limited_response(response, 100, "test")) == b'{"data":[]}'


def test_read_limited_response_rejects_stream_over_limit():
    response = FakeResponse([b"12345", b"67890"])

    with pytest.raises(SourceUnavailableError, match="size limit"):
        asyncio.run(read_limited_response(response, 8, "test"))


def test_read_limited_response_rejects_declared_size_over_limit():
    response = FakeResponse([], content_length=101)

    with pytest.raises(SourceUnavailableError, match="size limit"):
        asyncio.run(read_limited_response(response, 100, "test"))
