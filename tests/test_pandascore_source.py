import asyncio
from datetime import datetime, timezone

import pytest

from cs2bot.match_sources.filters import is_tier1_lan
from cs2bot.match_sources.models import SourceUnavailableError
from cs2bot.match_sources.sources import pandascore_source


def _sample_match():
    return {
        "id": 12345,
        "status": "finished",
        "number_of_games": 3,
        "begin_at": "2026-07-28T10:00:00Z",
        "end_at": "2026-07-28T12:10:00Z",
        "league": {"id": 1, "name": "IEM"},
        "serie": {"id": 2, "full_name": "IEM Cologne 2026"},
        "tournament": {"id": 3, "name": "Playoffs"},
        "opponents": [
            {"opponent": {"id": 10, "name": "NAVI"}},
            {"opponent": {"id": 20, "name": "FaZe"}},
        ],
        "results": [
            {"team_id": 20, "score": 1},
            {"team_id": 10, "score": 2},
        ],
    }


def _sample_upcoming():
    item = _sample_match()
    item["id"] = 67890
    item["status"] = "not_started"
    item["scheduled_at"] = "2026-07-30T11:00:00Z"
    item["end_at"] = None
    item["results"] = []
    return item


def test_pandascore_normalizes_series_result_by_team_id():
    matches = pandascore_source._normalize_raw_matches([_sample_match()])

    assert len(matches) == 1
    match = matches[0]
    assert match.source == "pandascore"
    assert match.match_id == "12345"
    assert match.team1_name == "NAVI"
    assert match.team2_name == "FaZe"
    assert (match.score1, match.score2) == (2, 1)
    assert match.status == "finished"
    assert match.best_of == 3
    assert match.tournament_name == "IEM — IEM Cologne 2026 — Playoffs"
    assert match.competition_key == "IEM Cologne 2026"
    assert match.start_date == "2026-07-28T10:00:00Z"
    assert match.end_date == "2026-07-28T12:10:00Z"
    assert match.maps == []


def test_pandascore_blast_bounty_finals_are_recognized_as_tier1_lan():
    item = _sample_match()
    item["league"] = {"id": 11, "name": "BLAST Bounty"}
    item["serie"] = {"id": 12, "full_name": "2026 Season 2 Finals"}
    item["tournament"] = {"id": 13, "name": "Playoffs"}
    item["opponents"] = [
        {"opponent": {"id": 10, "name": "3DMAX"}},
        {"opponent": {"id": 20, "name": "MOUZ"}},
    ]
    item["results"] = [
        {"team_id": 10, "score": 1},
        {"team_id": 20, "score": 2},
    ]

    match = pandascore_source._normalize_raw_matches([item])[0]

    assert match.tournament_name == "BLAST Bounty — 2026 Season 2 Finals — Playoffs"
    assert match.location is None
    assert match.is_lan is None
    assert is_tier1_lan(match) == (True, None)


def test_pandascore_blast_bounty_online_stage_is_selected():
    item = _sample_match()
    item["league"] = {"id": 11, "name": "BLAST Bounty"}
    item["serie"] = {"id": 12, "full_name": "2026 Season 2"}
    item["tournament"] = {"id": 13, "name": "Online Stage"}

    match = pandascore_source._normalize_raw_matches([item])[0]

    assert is_tier1_lan(match) == (True, None)


def test_pandascore_skips_incomplete_match_without_two_opponents():
    item = _sample_match()
    item["opponents"] = item["opponents"][:1]

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_skips_match_not_confirmed_finished():
    item = _sample_match()
    item["status"] = "running"

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_skips_unknown_best_of():
    item = _sample_match()
    item["number_of_games"] = 2

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_recent_range_avoids_undated_records(monkeypatch):
    monkeypatch.setattr(pandascore_source.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    monkeypatch.setattr(pandascore_source.source_config, "MAX_SOURCE_FUTURE_SKEW_HOURS", 6)

    result = pandascore_source._recent_match_range(
        datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    )

    assert result == "2026-07-22T14:00:00Z,2026-07-29T20:00:00Z"


def test_pandascore_requires_token(monkeypatch):
    monkeypatch.setattr(pandascore_source.source_config, "PANDASCORE_API_TOKEN", None)

    with pytest.raises(SourceUnavailableError, match="credentials"):
        asyncio.run(pandascore_source.fetch_finished_matches())


def test_pandascore_normalizes_featured_upcoming_match():
    matches = pandascore_source._normalize_raw_upcoming([_sample_upcoming()])

    assert len(matches) == 1
    match = matches[0]
    assert match.match_id == "67890"
    assert match.scheduled_at == "2026-07-30T11:00:00Z"
    assert match.is_featured is True
    assert match.feature_reason == "tier1_tournament"


def test_pandascore_upcoming_rejects_running_match():
    item = _sample_upcoming()
    item["status"] = "running"

    assert pandascore_source._normalize_raw_upcoming([item]) == []


def test_pandascore_utc_range_normalizes_timezone():
    result = pandascore_source._utc_range(
        datetime.fromisoformat("2026-07-30T00:00:00+03:00"),
        datetime.fromisoformat("2026-07-31T00:00:00+03:00"),
    )

    assert result == "2026-07-29T21:00:00Z,2026-07-30T21:00:00Z"
