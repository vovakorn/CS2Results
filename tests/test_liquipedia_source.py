import asyncio
import json

import pytest

from cs2bot.match_sources.models import SourceUnavailableError
from cs2bot.match_sources.sources import liquipedia_source


def _sample_response():
    return {
        "result": [
            {
                "pagename": "IEM Cologne 2026/Playoffs",
                "match2id": "abcdefghij_R01-M001",
                "tournament": "IEM Cologne 2026",
                "date": "2026-07-28T12:00:00+00:00",
                "type": "offline",
                "finished": "1",
                "bestof": "3",
                "dateexact": "1",
                "section": "Playoffs",
                "vod": "https://www.youtube.com/watch?v=example",
                "liquipediatier": "1",
                "liquipediatiertype": "General",
                "publishertier": "Major",
                "match2opponents": json.dumps(
                    [
                        {"name": "Natus Vincere", "score": "2", "status": "S"},
                        {"name": "FaZe Clan", "score": "1", "status": "S"},
                    ]
                ),
                "match2games": json.dumps(
                    [
                        {"map": "Mirage", "scores": [13, 9]},
                        {"map": "Nuke", "scores": [13, 11]},
                    ]
                ),
            }
        ]
    }


def test_liquipedia_normalizes_finished_offline_match():
    matches = liquipedia_source._normalize_raw_matches(_sample_response())

    assert len(matches) == 1
    match = matches[0]
    assert match.source == "liquipedia"
    assert match.match_id == "abcdefghij_R01-M001"
    assert match.team1_name == "Natus Vincere"
    assert match.team2_name == "FaZe Clan"
    assert (match.score1, match.score2) == (2, 1)
    assert match.status == "finished"
    assert match.best_of == 3
    assert match.competition_key == "IEM Cologne 2026"
    assert match.tournament_tier == "s"
    assert match.tournament_tier_type == "General"
    assert match.publisher_tier == "Major"
    assert match.tournament_section == "Playoffs"
    assert match.is_final is False
    assert match.date_exact is True
    assert match.vod_url == "https://www.youtube.com/watch?v=example"
    assert match.forfeit is False
    assert (match.team1_result_status, match.team2_result_status) == ("S", "S")
    assert match.is_lan is True
    assert match.match_url == "https://liquipedia.net/counterstrike/IEM_Cologne_2026/Playoffs"
    assert [(item.name, item.score1, item.score2) for item in match.maps] == [
        ("Mirage", 13, 9),
        ("Nuke", 13, 11),
    ]


def test_liquipedia_marks_online_match_as_not_lan():
    response = _sample_response()
    response["result"][0]["type"] = "online"

    assert liquipedia_source._normalize_raw_matches(response)[0].is_lan is False


def test_liquipedia_accepts_explicit_grand_final_only():
    response = _sample_response()
    response["result"][0]["section"] = "Grand Final"

    assert liquipedia_source._normalize_raw_matches(response)[0].is_final is True


def test_liquipedia_uses_winner_prize_only_for_matching_first_place_team():
    response = _sample_response()
    match = liquipedia_source._normalize_raw_matches(response)[0]
    placements = {
        "result": [
            {"placement": "1", "opponentname": "Natus Vincere", "prizemoney": "500000"},
            {"placement": "2", "opponentname": "FaZe Clan", "prizemoney": "170000"},
        ]
    }

    assert liquipedia_source._winner_prize_from_placements(match, placements) == 500_000


def test_liquipedia_does_not_guess_winner_prize_for_another_team():
    match = liquipedia_source._normalize_raw_matches(_sample_response())[0]
    placements = {"result": [{"placement": "1", "opponentname": "Team Spirit", "prizemoney": "500000"}]}

    assert liquipedia_source._winner_prize_from_placements(match, placements) is None


def test_liquipedia_skips_match_not_confirmed_finished():
    response = _sample_response()
    response["result"][0]["finished"] = "0"

    assert liquipedia_source._normalize_raw_matches(response) == []


def test_liquipedia_preserves_technical_result():
    response = _sample_response()
    opponents = json.loads(response["result"][0]["match2opponents"])
    opponents[1]["status"] = "FF"
    response["result"][0]["match2opponents"] = json.dumps(opponents)
    response["result"][0]["resulttype"] = "default"

    match = liquipedia_source._normalize_raw_matches(response)[0]

    assert match.forfeit is True
    assert match.result_type == "default"
    assert match.team2_result_status == "FF"


def test_liquipedia_uses_explicit_best_of_before_score_inference():
    response = _sample_response()
    response["result"][0]["bestof"] = "5"

    assert liquipedia_source._normalize_raw_matches(response)[0].best_of == 5


def test_liquipedia_requires_api_key(monkeypatch):
    monkeypatch.setattr(liquipedia_source.source_config, "LIQUIPEDIA_API_KEY", None)

    with pytest.raises(SourceUnavailableError, match="credentials"):
        asyncio.run(liquipedia_source.fetch_finished_matches())
