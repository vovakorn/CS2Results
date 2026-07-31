from __future__ import annotations

import re

from .config import (
    FEATURED_TIER2_TOURNAMENT_PATTERNS,
    ONLINE_LOCATION_MARKERS,
    POPULAR_TEAMS,
    TEAM_EXCLUSION_PATTERNS,
    TIER1_PRIZE_POOL_THRESHOLD_USD,
    TIER1_TOURNAMENT_PATTERNS,
    TOURNAMENT_EXCLUSION_PATTERNS,
    TRUSTED_LAN_TOURNAMENT_PHASE_PATTERNS,
    TRUSTED_LAN_TOURNAMENT_PATTERNS,
)
from .models import MatchNormalized, UpcomingMatchNormalized


def _contains_pattern(value: str | None, patterns: list[str]) -> bool:
    if not value:
        return False
    lowered = value.casefold()
    return any(pattern.casefold() in lowered for pattern in patterns)


def _matches_trusted_lan_phase(tournament_name: str | None) -> bool:
    return any(
        _contains_pattern(tournament_name, [tournament_pattern])
        and _contains_pattern(tournament_name, phase_patterns)
        for tournament_pattern, phase_patterns in TRUSTED_LAN_TOURNAMENT_PHASE_PATTERNS.items()
    )


def is_tier1_candidate(match: MatchNormalized) -> bool:
    """Return whether the event looks Tier-1 independently of its LAN evidence."""
    return bool(
        (
            match.prize_pool_usd is not None
            and match.prize_pool_usd >= TIER1_PRIZE_POOL_THRESHOLD_USD
        )
        or _contains_pattern(match.tournament_name, TIER1_TOURNAMENT_PATTERNS)
    )


def is_valid_match(match: MatchNormalized) -> tuple[bool, str | None]:
    if not match.team1_name:
        return False, "missing_team1"
    if not match.team2_name:
        return False, "missing_team2"
    if match.score1 is None or match.score2 is None:
        return False, "missing_score"
    if match.status != "finished":
        return False, "match_not_finished"
    if match.score1 == 0 and match.score2 == 0:
        return False, "empty_score"
    if match.score1 == match.score2:
        return False, "winner_unconfirmed"
    if match.best_of is not None:
        wins_required = (match.best_of // 2) + 1
        winner_score = max(match.score1, match.score2)
        loser_score = min(match.score1, match.score2)
        if winner_score != wins_required or loser_score >= wins_required:
            return False, "invalid_best_of_score"
    elif max(match.score1, match.score2) > 3:
        return False, "implausible_series_score"
    if not match.tournament_name:
        return False, "missing_tournament"
    if not match.match_id and not match.match_url:
        return False, "missing_stable_identifier"
    return True, None


def detect_operator(tournament_name: str) -> str | None:
    if not tournament_name:
        return None

    rules = [
        (r"\bIEM\b", "IEM"),
        (r"\bESL\b", "ESL"),
        (r"\bPGL\b", "PGL"),
        (r"\bBLAST\b", "BLAST"),
        (r"\bEsports World Cup\b", "Esports World Cup"),
        (r"\bFISSURE\b", "FISSURE"),
    ]
    for pattern, operator in rules:
        if re.search(pattern, tournament_name, flags=re.IGNORECASE):
            return operator
    return None


def is_tier1_lan(match: MatchNormalized) -> tuple[bool, str | None]:
    location = match.location or ""
    location_lower = location.casefold()
    tournament_lower = match.tournament_name.casefold()
    excluded = _contains_pattern(match.tournament_name, TOURNAMENT_EXCLUSION_PATTERNS)
    excluded_team = _contains_pattern(
        f"{match.team1_name} {match.team2_name}",
        TEAM_EXCLUSION_PATTERNS,
    )

    if excluded_team:
        return False, "excluded_team"

    if match.is_lan is False:
        lan_confirmed = False
        lan_reason = "explicitly_not_lan"
    elif match.is_lan is True:
        lan_confirmed = True
        lan_reason = None
    elif any(marker in location_lower for marker in ONLINE_LOCATION_MARKERS):
        lan_confirmed = False
        lan_reason = "online_location"
    elif any(marker in tournament_lower for marker in ONLINE_LOCATION_MARKERS):
        lan_confirmed = False
        lan_reason = "online_tournament"
    elif excluded:
        lan_confirmed = False
        lan_reason = "excluded_tournament"
    elif location.strip():
        lan_confirmed = True
        lan_reason = None
    elif _contains_pattern(
        match.tournament_name,
        TRUSTED_LAN_TOURNAMENT_PATTERNS,
    ) or _matches_trusted_lan_phase(match.tournament_name):
        lan_confirmed = True
        lan_reason = None
    else:
        lan_confirmed = False
        lan_reason = "lan_unconfirmed"

    tier1_confirmed = is_tier1_candidate(match)

    if lan_confirmed and tier1_confirmed:
        return True, None
    if not lan_confirmed:
        return False, lan_reason
    return False, "not_tier1"


def is_featured_upcoming(match: UpcomingMatchNormalized) -> tuple[bool, str | None]:
    """Include Tier-1 events and notable teams, excluding low-signal fixtures."""
    if _contains_pattern(match.tournament_name, TOURNAMENT_EXCLUSION_PATTERNS):
        return False, "excluded_tournament"
    if _contains_pattern(
        f"{match.team1_name} {match.team2_name}",
        TEAM_EXCLUSION_PATTERNS,
    ):
        return False, "excluded_team"
    if _contains_pattern(match.tournament_name, TIER1_TOURNAMENT_PATTERNS):
        return True, "tier1_tournament"

    normalized_teams = {
        MatchNormalized._identity_part(match.team1_name),
        MatchNormalized._identity_part(match.team2_name),
    }
    normalized_popular = {
        MatchNormalized._identity_part(team)
        for team in POPULAR_TEAMS
    }
    if (
        normalized_teams & normalized_popular
        and _contains_pattern(match.tournament_name, FEATURED_TIER2_TOURNAMENT_PATTERNS)
    ):
        return True, "popular_team"
    return False, "not_featured"
