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
