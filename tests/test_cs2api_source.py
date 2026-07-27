import asyncio
import json
from pathlib import Path

from cs2bot.match_sources.models import MatchNormalized
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


def test_cs2api_source_uses_direct_http_adapter(monkeypatch):
    async def fake_http():
        return [_match("2")]

    monkeypatch.setattr(cs2api_source, "_fetch_via_bo3_http", fake_http)

    matches = asyncio.run(cs2api_source.fetch_finished_matches(limit=10))

    assert [match.match_id for match in matches] == ["2"]


def test_cs2api_source_applies_requested_limit(monkeypatch):
    async def fake_http():
        return [_match("1"), _match("2"), _match("3")]

    monkeypatch.setattr(cs2api_source, "_fetch_via_bo3_http", fake_http)

    matches = asyncio.run(cs2api_source.fetch_finished_matches(limit=2))

    assert [match.match_id for match in matches] == ["1", "2"]


def test_bo3_fixture_normalizes_finished_matches():
    fixture = Path(__file__).parent / "fixtures" / "bo3_finished_matches_sample.json"
    data = json.loads(fixture.read_text())

    matches = cs2api_source._normalize_raw_matches(data)

    assert len(matches) == 2
    assert matches[0].source == "cs2api"
    assert matches[0].match_id == "984321"
    assert matches[0].team1_name == "NAVI"
    assert matches[0].team2_name == "FaZe"
    assert matches[0].score1 == 2
    assert matches[0].score2 == 1
    assert matches[0].tournament_name == "IEM Cologne 2026"
    assert matches[0].date == "2026-02-17T12:40:00.000+00:00"
    assert matches[0].start_date == "2026-02-17T10:30:00.000+00:00"
    assert matches[0].end_date == "2026-02-17T12:40:00.000+00:00"
    assert matches[0].location == "Cologne, Germany"
    assert matches[0].prize_pool_usd == 1000000
    assert matches[0].operator == "ESL"
    assert [item.name for item in matches[0].maps] == ["Mirage", "Ancient", "Inferno"]
    assert [(item.score1, item.score2) for item in matches[0].maps] == [(13, 11), (7, 13), (13, 10)]


def test_bad_item_does_not_abort_other_source_results():
    valid = {
        "id": "2",
        "team1": "NAVI",
        "team2": "FaZe",
        "team1_score": 2,
        "team2_score": 1,
        "tournament_name": "IEM Test",
        "prize_pool_usd": "$1,000,000",
    }
    malformed = {
        "id": "1",
        "team1": "Broken",
        "team2": "Data",
        "team1_score": 2,
        "team2_score": 1,
        "tournament_name": "Broken",
        "is_lan": "not-a-boolean",
    }

    matches = cs2api_source._normalize_raw_matches([malformed, valid])

    assert [match.match_id for match in matches] == ["2"]
    assert matches[0].prize_pool_usd == 1000000
