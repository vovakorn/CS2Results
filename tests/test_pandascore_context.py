import asyncio

from cs2bot.match_sources.sources import pandascore_context


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
        if path.endswith("/standings"):
            return [{"team": {"id": 10, "name": "NAVI"}, "rank": 1}]
        return []

    monkeypatch.setattr(pandascore_context.pandascore_source, "_fetch_json", fake_fetch)

    radar = asyncio.run(pandascore_context.fetch_tournament_radar("3"))

    assert radar.standings == ["1. NAVI"]
    assert radar.bracket_matches == []
