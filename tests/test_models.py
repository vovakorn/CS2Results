import pytest

from cs2bot.match_sources.models import MatchNormalized


def _match(**kwargs):
    data = {
        "source": "hltv",
        "match_id": "123456",
        "match_url": "https://www.hltv.org/matches/123456/example",
        "tournament_name": "IEM Cologne 2026",
        "team1_name": "NAVI",
        "team2_name": "FaZe",
        "score1": 2,
        "score2": 1,
    }
    data.update(kwargs)
    return MatchNormalized(**data)


def test_match_uid_uses_match_id():
    assert _match().match_uid == "hltv_123456"


def test_match_uid_uses_match_url_when_id_missing():
    match = _match(match_id=None, match_url="https://www.hltv.org/matches/123456/example-match")
    assert match.match_uid == "hltv_example-match"


def test_match_uid_requires_stable_identifier():
    match = _match(match_id=None, match_url=None)
    with pytest.raises(ValueError):
        _ = match.match_uid


def test_match_accepts_start_and_end_dates():
    match = _match(start_date="2026-02-17T10:30:00Z", end_date="2026-02-17T12:40:00Z")
    assert match.start_date == "2026-02-17T10:30:00Z"
    assert match.end_date == "2026-02-17T12:40:00Z"
