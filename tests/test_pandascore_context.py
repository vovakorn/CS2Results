import asyncio
from datetime import datetime, timedelta, timezone

from cs2bot.match_sources.sources import pandascore_context
from cs2bot.match_sources.models import MatchNormalized, SourceReferences


def test_tournament_radar_extracts_ranked_standings():
    data = [
        {"team": {"id": 10, "name": "NAVI"}, "rank": 1},
        {"team": {"id": 20, "name": "FaZe"}, "position": 2},
    ]

    assert pandascore_context._standing_lines(data) == ["1. NAVI", "2. FaZe"]


def test_tournament_radar_counts_each_bracket_match_once():
    data = [
        {
            "match_id": 101,
            "match": {"id": 101},
            "children": [{"match": {"id": 102}}],
        }
    ]

    assert pandascore_context._bracket_match_count(data) == 2


def test_tournament_radar_extracts_only_explicit_bracket_pairs():
    data = [{"round": "Semifinal", "match": {"id": 101, "opponents": [
        {"opponent": {"name": "NAVI"}}, {"opponent": {"name": "FaZe"}}
    ]}}]

    pairs = pandascore_context._bracket_matches(data)

    assert [(pair.team1_name, pair.team2_name, pair.round_name) for pair in pairs] == [
        ("NAVI", "FaZe", "Semifinal")
    ]


def test_tournament_radar_keeps_available_data_when_one_endpoint_fails(monkeypatch):
    async def fake_fetch(path, params):
        if path.endswith("/brackets"):
            raise RuntimeError("unavailable")
        if path.endswith("/rosters"):
            return [{"team": {"id": 10, "name": "NAVI"}}]
        return []

    monkeypatch.setattr(pandascore_context.pandascore_source, "_fetch_json", fake_fetch)

    radar = asyncio.run(pandascore_context.fetch_tournament_radar("3"))

    assert radar.roster_team_count == 1
    assert radar.bracket_matches == []


def test_schedule_context_extracts_recent_head_to_head_from_team_history():
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    matches = [
        MatchNormalized(
            source="pandascore", match_id=str(index), tournament_name="IEM", team1_name="NAVI",
            team2_name="FaZe", score1=2 if index != 2 else 1, score2=1 if index != 2 else 2,
            end_date=(now - timedelta(days=index)).isoformat(),
            source_refs=SourceReferences(team1_id="10", team2_id="20"),
        )
        for index in range(3)
    ]

    record = pandascore_context._head_to_head("10", "20", matches, now=now)

    assert record is not None
    assert (record.match_count, record.team1_wins, record.team2_wins) == (3, 2, 1)


def test_schedule_context_excludes_head_to_head_older_than_three_months():
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    matches = [
        MatchNormalized(
            source="pandascore", match_id=str(index), tournament_name="IEM", team1_name="NAVI",
            team2_name="FaZe", score1=2 if index == 0 else 1, score2=1 if index == 0 else 2,
            end_date=(now - timedelta(days=days_ago)).isoformat(),
            source_refs=SourceReferences(team1_id="10", team2_id="20"),
        )
        for index, days_ago in enumerate((1, 20, 91))
    ]

    record = pandascore_context._head_to_head("10", "20", matches, now=now)

    assert record is not None
    assert (record.match_count, record.team1_wins, record.team2_wins) == (2, 1, 1)


def test_schedule_context_keeps_all_recent_head_to_head_matches():
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    matches = [
        MatchNormalized(
            source="pandascore", match_id=str(index), tournament_name="IEM", team1_name="NAVI",
            team2_name="FaZe", score1=2, score2=1,
            end_date=(now - timedelta(days=index)).isoformat(),
            source_refs=SourceReferences(team1_id="10", team2_id="20"),
        )
        for index in range(5)
    ]

    record = pandascore_context._head_to_head("10", "20", matches, now=now)

    assert (record.match_count, record.team1_wins, record.team2_wins) == (5, 5, 0)
