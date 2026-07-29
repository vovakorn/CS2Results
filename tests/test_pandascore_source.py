import asyncio

import pytest

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


def test_pandascore_requires_token(monkeypatch):
    monkeypatch.setattr(pandascore_source.source_config, "PANDASCORE_API_TOKEN", None)

    with pytest.raises(SourceUnavailableError, match="credentials"):
        asyncio.run(pandascore_source.fetch_finished_matches())
