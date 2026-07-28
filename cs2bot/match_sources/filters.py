from __future__ import annotations

import re

from .config import (
    ONLINE_LOCATION_MARKERS,
    TIER1_PRIZE_POOL_THRESHOLD_USD,
    TIER1_TOURNAMENT_PATTERNS,
    TOURNAMENT_EXCLUSION_PATTERNS,
    TRUSTED_LAN_TOURNAMENT_PATTERNS,
)
from .models import MatchNormalized


def _contains_pattern(value: str | None, patterns: list[str]) -> bool:
    if not value:
        return False
    lowered = value.casefold()
    return any(pattern.casefold() in lowered for pattern in patterns)


def is_valid_match(match: MatchNormalized) -> tuple[bool, str | None]:
    if not match.team1_name:
        return False, "missing_team1"
    if not match.team2_name:
        return False, "missing_team2"
    if match.score1 is None or match.score2 is None:
        return False, "missing_score"
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
    excluded = _contains_pattern(match.tournament_name, TOURNAMENT_EXCLUSION_PATTERNS)

    if match.is_lan is False:
        lan_confirmed = False
        lan_reason = "explicitly_not_lan"
    elif match.is_lan is True:
        lan_confirmed = True
        lan_reason = None
    elif any(marker in location_lower for marker in ONLINE_LOCATION_MARKERS):
        lan_confirmed = False
        lan_reason = "online_location"
    elif excluded:
        lan_confirmed = False
        lan_reason = "excluded_tournament"
    elif location.strip():
        lan_confirmed = True
        lan_reason = None
    elif _contains_pattern(match.tournament_name, TRUSTED_LAN_TOURNAMENT_PATTERNS):
        lan_confirmed = True
        lan_reason = None
    else:
        lan_confirmed = False
        lan_reason = "lan_unconfirmed"

    tier1_confirmed = False
    if match.prize_pool_usd is not None and match.prize_pool_usd >= TIER1_PRIZE_POOL_THRESHOLD_USD:
        tier1_confirmed = True
    elif _contains_pattern(match.tournament_name, TIER1_TOURNAMENT_PATTERNS):
        tier1_confirmed = True

    if lan_confirmed and tier1_confirmed:
        return True, None
    if not lan_confirmed:
        return False, lan_reason
    return False, "not_tier1"
