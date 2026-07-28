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
                "liquipediatier": "1",
                "match2opponents": json.dumps(
                    [
                        {"name": "Natus Vincere", "score": "2"},
                        {"name": "FaZe Clan", "score": "1"},
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
    assert match.competition_key == "IEM Cologne 2026"
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


def test_liquipedia_requires_api_key(monkeypatch):
    monkeypatch.setattr(liquipedia_source.source_config, "LIQUIPEDIA_API_KEY", None)

    with pytest.raises(SourceUnavailableError, match="credentials"):
        asyncio.run(liquipedia_source.fetch_finished_matches())
