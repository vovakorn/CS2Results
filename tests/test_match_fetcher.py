import asyncio
import logging
from datetime import datetime, timezone

import pytest

from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.models import MapResult, MatchNormalized, SourceUnavailableError


def _match(source="pandascore", match_id="1", tournament_name="IEM Cologne 2026"):
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


def test_auto_uses_pandascore_when_it_returns_matches(monkeypatch):
    async def fake_fetch(source, limit):
        assert source == "pandascore"
        return [_match()]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "pandascore"


def test_liquipedia_shadow_timeout_does_not_block_primary_results(monkeypatch, caplog):
    async def fake_fetch(source, limit):
        assert source == "pandascore"
        return [_match()]

    async def slow_shadow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"matched": 1}

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    monkeypatch.setattr(match_fetcher, "_run_liquipedia_shadow", slow_shadow)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_SHADOW_TIMEOUT_SECONDS", 0.001)

    with caplog.at_level(logging.WARNING):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(source="pandascore", dry_run=True)
        )

    assert matches[0].source == "pandascore"
    assert "event=liquipedia_shadow_timed_out" in caplog.text


def test_auto_falls_back_to_liquipedia_when_pandascore_empty(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "pandascore":
            return []
        return [_match(source="liquipedia", match_id="2")]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "liquipedia"


def test_auto_falls_back_to_liquipedia_when_pandascore_unavailable(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "pandascore":
            raise SourceUnavailableError("down")
        return [_match(source="liquipedia", match_id="2")]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "liquipedia"


def test_auto_does_not_fall_back_to_liquipedia_when_fallback_disabled(monkeypatch):
    calls = []

    async def fake_fetch(source, limit):
        calls.append(source)
        return []

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))

    assert matches == []
    assert calls == ["pandascore"]


def test_auto_raises_when_pandascore_unavailable_and_fallback_disabled(monkeypatch):
    async def fake_fetch(source, limit):
        raise SourceUnavailableError("down")

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with pytest.raises(SourceUnavailableError):
        asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))


def test_auto_falls_back_when_primary_is_stale_in_production(monkeypatch):
    calls = []
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        calls.append(source)
        match = _match(source=source, match_id="1" if source == "pandascore" else "2")
        match.end_date = "2000-01-01T00:00:00Z" if source == "pandascore" else recent
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    used_source, matches = asyncio.run(
        match_fetcher._choose_source("auto", 10, require_fresh=True)
    )

    assert used_source == "liquipedia"
    assert matches[0].source == "liquipedia"
    assert calls == ["pandascore", "liquipedia"]


def test_auto_never_returns_stale_primary_when_fallback_fails(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "liquipedia":
            raise SourceUnavailableError("blocked")
        match = _match()
        match.end_date = "2000-01-01T00:00:00Z"
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with pytest.raises(SourceUnavailableError, match="no usable match source"):
        asyncio.run(match_fetcher._choose_source("auto", 10, require_fresh=True))


def test_auto_falls_back_when_primary_has_no_valid_matches(monkeypatch):
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        match = _match(source=source, match_id="1" if source == "pandascore" else "2")
        match.end_date = recent
        if source == "pandascore":
            match.score1 = None
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    used_source, _ = asyncio.run(
        match_fetcher._choose_source("auto", 10, require_fresh=True)
    )
    assert used_source == "liquipedia"


def test_production_drops_stale_matches_even_when_source_has_recent_data(monkeypatch):
    stale_tier1 = _match(match_id="old")
    stale_tier1.end_date = "2000-01-01T00:00:00Z"
    recent_non_tier1 = _match(match_id="new", tournament_name="Regional Finals")
    recent_non_tier1.end_date = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        return [stale_tier1, recent_non_tier1]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(
            source="pandascore",
            dry_run=False,
            check_processed=False,
        )
    )

    assert matches == []


def test_include_filtered_returns_filtered_reason(monkeypatch):
    async def fake_fetch(source, limit):
        return [_match(source="liquipedia", tournament_name="Regional Finals")]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(source="liquipedia", dry_run=True, include_filtered=True)
    )
    assert matches[0].is_tier1_lan is False
    assert matches[0].filter_reason == "not_tier1"


def test_pandascore_tier_is_logged_as_shadow_diagnostic(caplog):
    selected = _match(source="pandascore", tournament_name="IEM Cologne 2026")
    selected.tournament_tier = "s"
    rejected = _match(source="pandascore", tournament_name="Regional Finals")
    rejected.tournament_tier = "a"

    with caplog.at_level(logging.INFO):
        output, valid, selected_count = match_fetcher.apply_quality_filters(
            [selected, rejected]
        )

    assert output == [selected]
    assert valid == [selected, rejected]
    assert selected_count == 1
    assert "event=pandascore_tier_diagnostics" in caplog.text
    assert "s_selected=1" in caplog.text
    assert "a_rejected=1" in caplog.text


def test_liquipedia_shadow_comparison_aligns_reversed_team_order():
    primary = _match()
    primary.date = "2026-08-09T12:00:00Z"
    primary.tournament_tier = "s"

    shadow = _match(source="liquipedia", match_id="lp-1")
    shadow.date = "2026-08-09T11:55:00Z"
    shadow.team1_name = "FaZe Clan"
    shadow.team2_name = "Natus Vincere"
    shadow.score1 = 1
    shadow.score2 = 2
    shadow.tournament_tier = "a"
    shadow.maps = [MapResult(name="Mirage", score1=13, score2=9)]
    shadow.forfeit = True

    comparison = match_fetcher.compare_source_matches([primary], [shadow])

    assert comparison == {
        "primary_count": 1,
        "liquipedia_count": 1,
        "matched": 1,
        "primary_only": 0,
        "liquipedia_only": 0,
        "score_mismatches": 0,
        "best_of_mismatches": 0,
        "tier_mismatches": 1,
        "liquipedia_map_coverage": 1,
        "liquipedia_technical_results": 1,
    }


def test_liquipedia_shadow_comparison_uses_aliases_and_start_time():
    primary = _match()
    primary.team1_name = "1WIN"
    primary.team2_name = "Liquid"
    primary.start_date = "2026-08-08T23:45:00Z"
    primary.end_date = "2026-08-09T00:50:00Z"
    primary.date = primary.end_date

    shadow = _match(source="liquipedia", match_id="lp-1")
    shadow.team1_name = "1w Team"
    shadow.team2_name = "Team Liquid"
    shadow.start_date = "2026-08-08 23:45:00"
    shadow.date = shadow.start_date
    shadow.score1 = 2
    shadow.score2 = 1

    comparison = match_fetcher.compare_source_matches([primary], [shadow])

    assert comparison["matched"] == 1
    assert comparison["primary_only"] == 0
    assert comparison["liquipedia_only"] == 0
    assert comparison["score_mismatches"] == 0


def test_liquipedia_shadow_logs_comparison_without_changing_primary(monkeypatch, caplog):
    calls = []
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        calls.append(source)
        match = _match(source=source, match_id="1" if source == "pandascore" else "lp-1")
        match.date = recent
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_SHADOW", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "liquipedia-key")
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    diagnostics = {}

    with caplog.at_level(logging.INFO):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(
                source="auto",
                dry_run=True,
                shadow_diagnostics=diagnostics,
            )
        )

    assert matches[0].source == "pandascore"
    assert calls == ["pandascore", "liquipedia"]
    assert "event=liquipedia_shadow_comparison" in caplog.text
    assert "matched=1" in caplog.text
    assert diagnostics["matched"] == 1
    assert diagnostics["score_mismatches"] == 0


def test_liquipedia_shadow_failure_never_blocks_primary(monkeypatch, caplog):
    async def fake_fetch(source, limit):
        if source == "liquipedia":
            raise SourceUnavailableError("down")
        match = _match()
        match.date = datetime.now(timezone.utc).isoformat()
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_SHADOW", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "liquipedia-key")
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with caplog.at_level(logging.WARNING):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(source="auto", dry_run=True)
        )

    assert matches[0].source == "pandascore"
    assert "event=liquipedia_shadow_failed" in caplog.text


def test_configured_online_tier1_stage_enters_public_output(monkeypatch):
    selected = _match(
        source="pandascore",
        tournament_name="BLAST Bounty — 2026 Season 2 — Online Stage",
    )
    selected.location = None
    selected.end_date = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        return [selected]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    diagnostics = []

    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(
            source="pandascore",
            dry_run=False,
            check_processed=False,
            rejected_matches=diagnostics,
        )
    )

    assert matches == [selected]
    assert selected.is_tier1_lan is True
    assert selected.filter_reason is None
    assert diagnostics == []


def test_log_source_freshness_warns_when_latest_match_is_stale(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    match = _match()
    match.end_date = "2026-05-24T10:00:00Z"

    with caplog.at_level(logging.WARNING):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "pandascore",
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
            "pandascore",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is True
    assert age_hours == 1
    assert "event=source_fresh" in caplog.text


def test_log_source_freshness_rejects_future_timestamp(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_FUTURE_SKEW_HOURS", 6)
    match = _match()
    match.end_date = "2026-05-29T10:00:00Z"

    with caplog.at_level(logging.WARNING):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "pandascore",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is False
    assert age_hours == -24
    assert "event=source_future_timestamp" in caplog.text


def test_freshness_uses_latest_of_end_date_date_and_start_date():
    match = _match()
    match.end_date = "2026-05-24T10:00:00Z"
    match.date = "2026-05-28T09:00:00Z"
    match.start_date = "2026-05-27T10:00:00Z"
    reference = datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc)

    assert match_fetcher.latest_match_datetime([match]) == datetime(
        2026, 5, 28, 9, 0, tzinfo=timezone.utc
    )
    assert match_fetcher.is_match_fresh(match, now=reference) is True
