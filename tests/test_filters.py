import pytest
from pydantic import ValidationError

from cs2bot.match_sources.filters import (
    detect_operator,
    is_featured_upcoming,
    is_tier1_candidate,
    is_tier1_lan,
    is_valid_match,
)
from cs2bot.match_sources.models import MatchNormalized, UpcomingMatchNormalized


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


def _upcoming(tournament_name, team1_name="NAVI", team2_name="FaZe"):
    return UpcomingMatchNormalized(
        match_id="upcoming-1",
        tournament_name=tournament_name,
        team1_name=team1_name,
        team2_name=team2_name,
        scheduled_at="2026-07-30T11:00:00Z",
    )


def test_valid_match_passes():
    assert is_valid_match(_match()) == (True, None)


def test_match_without_team_fails():
    with pytest.raises(ValidationError):
        _match(team1_name="")


def test_match_without_score_fails():
    assert is_valid_match(_match(score1=None)) == (False, "missing_score")


def test_zero_zero_result_is_rejected():
    assert is_valid_match(_match(score1=0, score2=0)) == (False, "empty_score")


def test_tied_result_without_winner_is_rejected():
    assert is_valid_match(_match(score1=1, score2=1)) == (False, "winner_unconfirmed")


@pytest.mark.parametrize(
    ("best_of", "score1", "score2"),
    [(1, 1, 0), (3, 2, 0), (3, 2, 1), (5, 3, 0), (5, 3, 2)],
)
def test_valid_best_of_scores_pass(best_of, score1, score2):
    assert is_valid_match(_match(best_of=best_of, score1=score1, score2=score2)) == (True, None)


@pytest.mark.parametrize(
    ("best_of", "score1", "score2"),
    [(1, 2, 0), (3, 1, 0), (3, 3, 0), (5, 2, 1), (5, 4, 2)],
)
def test_invalid_best_of_scores_fail(best_of, score1, score2):
    assert is_valid_match(_match(best_of=best_of, score1=score1, score2=score2)) == (
        False,
        "invalid_best_of_score",
    )


def test_implausible_unknown_series_score_is_rejected():
    assert is_valid_match(_match(score1=13, score2=11)) == (
        False,
        "implausible_series_score",
    )


def test_iem_is_tier1_lan():
    assert is_tier1_lan(_match(tournament_name="IEM Cologne 2026")) == (True, None)


def test_tier1_event_without_lan_evidence_is_rejected():
    match = _match(tournament_name="Unlisted Arena Finals 2026", location=None, prize_pool_usd=1000000)
    assert is_tier1_lan(match) == (False, "lan_unconfirmed")


def test_explicit_lan_flag_confirms_lan_without_location():
    match = _match(
        tournament_name="CS Asia Championships 2026",
        location=None,
        prize_pool_usd=1000000,
        is_lan=True,
    )
    assert is_tier1_lan(match) == (True, None)


def test_explicit_not_lan_overrides_tournament_heuristics():
    match = _match(tournament_name="IEM Online Qualifier", location=None, is_lan=False)
    assert is_tier1_lan(match) == (False, "explicitly_not_lan")


def test_trusted_lan_tournament_passes_without_location():
    match = _match(tournament_name="IEM Cologne 2026", location=None)
    assert is_tier1_lan(match) == (True, None)


def test_blast_bounty_finals_pass_without_location():
    match = _match(
        tournament_name="BLAST Bounty — 2026 Season 2 Finals — Playoffs",
        location=None,
    )

    assert is_tier1_candidate(match) is True
    assert is_tier1_lan(match) == (True, None)


def test_blast_bounty_online_stage_does_not_inherit_final_lan_status():
    match = _match(
        tournament_name="BLAST Bounty — 2026 Season 2 — Online Stage",
        location=None,
    )

    assert is_tier1_candidate(match) is True
    assert is_tier1_lan(match) == (False, "online_tournament")


def test_qualifier_is_rejected_even_if_name_contains_trusted_event():
    match = _match(tournament_name="IEM Cologne 2026 Closed Qualifier", location=None)
    assert is_tier1_lan(match) == (False, "excluded_tournament")


@pytest.mark.parametrize("team_name", ["NAVI Academy", "Spirit Junior", "FaZe Youth"])
def test_academy_and_youth_teams_are_rejected(team_name):
    match = _match(team1_name=team_name)
    assert is_tier1_lan(match) == (False, "excluded_team")


def test_online_cup_does_not_pass_lan_filter():
    ok, reason = is_tier1_lan(_match(tournament_name="Online Cup", location="Online"))
    assert ok is False
    assert reason == "online_location"


def test_prize_pool_threshold_passes_tier1():
    match = _match(tournament_name="Big Arena Finals", prize_pool_usd=500000)
    assert is_tier1_lan(match) == (True, None)


def test_known_operator_alone_does_not_make_event_tier1():
    match = _match(
        tournament_name="Regional Challenger Finals",
        operator="ESL",
        location="Cologne, Germany",
        prize_pool_usd=100000,
    )
    assert is_tier1_lan(match) == (False, "not_tier1")


def test_missing_location_and_whitelist_does_not_pass_lan():
    ok, reason = is_tier1_lan(_match(tournament_name="Regional Finals", location=None, operator=None))
    assert ok is False
    assert reason == "lan_unconfirmed"


def test_known_operators_are_detected():
    assert detect_operator("IEM Cologne 2026") == "IEM"
    assert detect_operator("ESL Pro League Season 23") == "ESL"
    assert detect_operator("PGL Major Singapore 2026") == "PGL"
    assert detect_operator("BLAST Open Rotterdam 2026") == "BLAST"
    assert detect_operator("Esports World Cup 2026") == "Esports World Cup"
    assert detect_operator("FISSURE Playground") == "FISSURE"


def test_upcoming_tier1_event_is_featured():
    assert is_featured_upcoming(_upcoming("BLAST Bounty — Summer 2026")) == (
        True,
        "tier1_tournament",
    )


def test_popular_team_in_small_event_is_not_featured():
    assert is_featured_upcoming(_upcoming("CIS LAN Championship — Season 6")) == (
        False,
        "not_featured",
    )


def test_popular_team_in_named_tier2_event_is_featured():
    assert is_featured_upcoming(_upcoming("CCT Season 4 Europe")) == (
        True,
        "popular_team",
    )


def test_tier2_event_without_popular_team_is_not_featured():
    assert is_featured_upcoming(
        _upcoming("CCT Season 4 Europe", team1_name="Team A", team2_name="Team B")
    ) == (False, "not_featured")
