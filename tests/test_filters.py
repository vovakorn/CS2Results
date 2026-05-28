from cs2bot.match_sources.filters import detect_operator, is_tier1_lan, is_valid_match
from cs2bot.match_sources.models import MatchNormalized


def _match(**kwargs):
    data = {
        "source": "hltv",
        "match_id": "1",
        "match_url": "https://www.hltv.org/matches/1/test",
        "tournament_name": "IEM Cologne 2026",
        "team1_name": "NAVI",
        "team2_name": "FaZe",
        "score1": 2,
        "score2": 1,
        "location": "Cologne, Germany",
    }
    data.update(kwargs)
    return MatchNormalized(**data)


def test_valid_match_passes():
    assert is_valid_match(_match()) == (True, None)


def test_match_without_team_fails():
    assert is_valid_match(_match(team1_name="")) == (False, "missing_team1")


def test_match_without_score_fails():
    assert is_valid_match(_match(score1=None)) == (False, "missing_score")


def test_iem_is_tier1_lan():
    assert is_tier1_lan(_match(tournament_name="IEM Cologne 2026")) == (True, None)


def test_online_cup_does_not_pass_lan_filter():
    ok, reason = is_tier1_lan(_match(tournament_name="Online Cup", location="Online"))
    assert ok is False
    assert reason == "online_location"


def test_prize_pool_threshold_passes_tier1():
    match = _match(tournament_name="Big Arena Finals", prize_pool_usd=500000)
    assert is_tier1_lan(match) == (True, None)


def test_missing_location_and_whitelist_does_not_pass_lan():
    ok, reason = is_tier1_lan(_match(tournament_name="Regional Finals", location=None, operator=None))
    assert ok is False
    assert reason == "missing_location_and_prize_pool"


def test_known_operators_are_detected():
    assert detect_operator("IEM Cologne 2026") == "IEM"
    assert detect_operator("ESL Pro League Season 23") == "ESL"
    assert detect_operator("PGL Major Singapore 2026") == "PGL"
    assert detect_operator("BLAST Open Rotterdam 2026") == "BLAST"
    assert detect_operator("Esports World Cup 2026") == "Esports World Cup"
    assert detect_operator("FISSURE Playground") == "FISSURE"

