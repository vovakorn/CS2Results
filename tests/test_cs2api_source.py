import asyncio

from cs2bot.match_sources.models import MatchNormalized, SourceUnavailableError
from cs2bot.match_sources.sources import cs2api_source


def _match(match_id="1"):
    return MatchNormalized(
        source="cs2api",
        match_id=match_id,
        tournament_name="IEM Cologne 2026",
        team1_name="NAVI",
        team2_name="FaZe",
        score1=2,
        score2=1,
    )


def test_cs2api_source_falls_back_to_bo3_http_when_library_unavailable(monkeypatch):
    async def fake_library(limit=30):
        raise SourceUnavailableError("library failed")

    async def fake_http():
        return [_match("2")]

    monkeypatch.setattr(cs2api_source, "_fetch_via_cs2api_library", fake_library)
    monkeypatch.setattr(cs2api_source, "_fetch_via_bo3_http", fake_http)

    matches = asyncio.run(cs2api_source.fetch_finished_matches(limit=10))

    assert [match.match_id for match in matches] == ["2"]


def test_cs2api_source_uses_bo3_http_when_library_returns_empty(monkeypatch):
    async def fake_library(limit=30):
        return []

    async def fake_http():
        return [_match("3")]

    monkeypatch.setattr(cs2api_source, "_fetch_via_cs2api_library", fake_library)
    monkeypatch.setattr(cs2api_source, "_fetch_via_bo3_http", fake_http)

    matches = asyncio.run(cs2api_source.fetch_finished_matches(limit=10))

    assert [match.match_id for match in matches] == ["3"]
