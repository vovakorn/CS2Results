import asyncio
import logging
from datetime import datetime, timezone

import pytest

from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.models import MatchNormalized, SourceUnavailableError


def _match(source="cs2api", match_id="1", tournament_name="IEM Cologne 2026"):
    return MatchNormalized(
        source=source,
        match_id=match_id,
        match_url=f"https://example.com/{match_id}",
        tournament_name=tournament_name,
        team1_name="NAVI",
        team2_name="FaZe",
        score1=2,
        score2=1,
        location="Cologne, Germany",
    )


def test_auto_uses_cs2api_when_it_returns_matches(monkeypatch):
    async def fake_fetch(source, limit):
        assert source == "cs2api"
        return [_match()]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "cs2api"


def test_auto_falls_back_to_hltv_when_cs2api_empty(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "cs2api":
            return []
        return [_match(source="hltv", match_id="2")]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "hltv"


def test_auto_falls_back_to_hltv_when_cs2api_unavailable(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "cs2api":
            raise SourceUnavailableError("down")
        return [_match(source="hltv", match_id="2")]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "hltv"


def test_auto_does_not_fall_back_to_hltv_when_fallback_disabled(monkeypatch):
    calls = []

    async def fake_fetch(source, limit):
        calls.append(source)
        return []

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_HLTV_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))

    assert matches == []
    assert calls == ["cs2api"]


def test_auto_raises_when_cs2api_unavailable_and_fallback_disabled(monkeypatch):
    async def fake_fetch(source, limit):
        raise SourceUnavailableError("down")

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_HLTV_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with pytest.raises(SourceUnavailableError):
        asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))


def test_include_filtered_returns_filtered_reason(monkeypatch):
    async def fake_fetch(source, limit):
        return [_match(source="hltv", tournament_name="Regional Finals")]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(source="hltv", dry_run=True, include_filtered=True)
    )
    assert matches[0].is_tier1_lan is False
    assert matches[0].filter_reason == "not_tier1"


def test_log_source_freshness_warns_when_latest_match_is_stale(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    match = _match()
    match.end_date = "2026-05-24T10:00:00Z"

    with caplog.at_level(logging.WARNING):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "cs2api",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is False
    assert age_hours == 96
    assert "event=source_stale" in caplog.text


def test_log_source_freshness_accepts_recent_match(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    match = _match()
    match.end_date = "2026-05-28T09:00:00Z"

    with caplog.at_level(logging.INFO):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "cs2api",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is True
    assert age_hours == 1
    assert "event=source_fresh" in caplog.text
