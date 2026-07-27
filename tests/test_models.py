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


def test_legacy_match_uid_uses_source_match_id():
    assert _match().legacy_match_uid == "hltv_123456"


def test_legacy_match_uid_uses_match_url_when_id_missing():
    match = _match(match_id=None, match_url="https://www.hltv.org/matches/123456/example-match")
    assert match.legacy_match_uid == "hltv_example-match"


def test_match_uid_requires_stable_identifier():
    match = _match(match_id=None, match_url=None)
    with pytest.raises(ValueError):
        _ = match.match_uid


def test_match_accepts_start_and_end_dates():
    match = _match(start_date="2026-02-17T10:30:00Z", end_date="2026-02-17T12:40:00Z")
    assert match.start_date == "2026-02-17T10:30:00Z"
    assert match.end_date == "2026-02-17T12:40:00Z"


def test_canonical_uid_matches_across_sources_and_team_order():
    hltv = _match(date="2026-02-17", score1=2, score2=1)
    cs2api = _match(
        source="cs2api",
        match_id="984321",
        match_url="https://bo3.gg/matches/984321",
        date="2026-02-17T12:40:00Z",
        team1_name="FaZe",
        team2_name="NAVI",
        score1=1,
        score2=2,
    )
    assert hltv.match_uid.startswith("match_v1_")
    assert hltv.match_uid == cs2api.match_uid


def test_match_without_date_keeps_legacy_uid_to_avoid_false_collisions():
    assert _match(date=None).match_uid == "hltv_123456"


def test_match_rejects_negative_score():
    with pytest.raises(ValueError):
        _match(score1=-1)
