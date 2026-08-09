from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_TIER1_TOURNAMENT_PATTERNS = [
    "IEM",
    "ESL Pro League",
    "PGL",
    "BLAST Open",
    "BLAST Rivals",
    "BLAST Bounty",
    "Esports World Cup",
    "FISSURE Playground",
    "CS Asia Championships",
    "Major",
]
DEFAULT_FEATURED_TIER2_TOURNAMENT_PATTERNS = [
    "CCT",
    "Thunderpick World Championship",
    "BetBoom Dacha",
    "YaLLa Compass",
    "StarLadder",
    "RES Regional",
    "Skyesports Masters",
]

DEFAULT_ONLINE_LOCATION_MARKERS = ["online", "remote"]
DEFAULT_TRUSTED_LAN_TOURNAMENT_PATTERNS = [
    "Major",
    "IEM Cologne",
    "IEM Katowice",
    "IEM Dallas",
    "IEM Chengdu",
    "ESL Pro League",
    "BLAST Open",
    "BLAST Rivals",
    "Esports World Cup",
    "FISSURE Playground",
    "CS Asia Championships",
]
DEFAULT_TRUSTED_LAN_TOURNAMENT_PHASE_PATTERNS = {
    "BLAST Bounty": ["Finals", "Playoffs"],
}
DEFAULT_TRUSTED_ONLINE_TIER1_TOURNAMENT_PHASE_PATTERNS = {
    "BLAST Bounty": ["Online Stage"],
}
DEFAULT_TOURNAMENT_EXCLUSION_PATTERNS = [
    "qualifier",
    "open qualifier",
    "closed qualifier",
    "regional qualifier",
    "showmatch",
    "show match",
    "academy league",
    "youth league",
]
DEFAULT_TEAM_EXCLUSION_PATTERNS = [
    "academy",
    "youth",
    "junior",
]
DEFAULT_TEAM_ALIASES = {
    "natus vincere": "navi",
    "navi": "navi",
    "faze clan": "faze",
    "faze": "faze",
    "team spirit": "spirit",
    "spirit": "spirit",
    "team vitality": "vitality",
    "vitality": "vitality",
    "g2 esports": "g2",
    "g2": "g2",
}
DEFAULT_POPULAR_TEAMS = [
    "Natus Vincere",
    "NAVI",
    "Team Spirit",
    "Vitality",
    "MOUZ",
    "FaZe",
    "G2",
    "Falcons",
    "Team Liquid",
    "Liquid",
    "FURIA",
    "The MongolZ",
    "Astralis",
    "Virtus.pro",
    "Aurora",
    "HEROIC",
]


def _load_json_config(env_name: str) -> dict:
    raw = os.getenv(env_name)
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{env_name} must be a JSON object")
    return data


def _load_json_file(path: str | None) -> dict:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    return data


TIER1_FILTER_CONFIG = {
    **_load_json_file(os.getenv("TIER1_FILTER_CONFIG_PATH", "tier1_filter.json")),
    **_load_json_config("TIER1_FILTER_CONFIG_JSON"),
}


def _list_setting(name: str, default: list[str]) -> list[str]:
    value = TIER1_FILTER_CONFIG.get(name, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"TIER1_FILTER_CONFIG_JSON.{name} must be a string array")
    return value


def _dict_setting(name: str, default: dict[str, str]) -> dict[str, str]:
    value = TIER1_FILTER_CONFIG.get(name, default)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"TIER1_FILTER_CONFIG_JSON.{name} must be a string-to-string object")
    return value


def _dict_list_setting(name: str, default: dict[str, list[str]]) -> dict[str, list[str]]:
    value = TIER1_FILTER_CONFIG.get(name, default)
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and isinstance(items, list)
        and all(isinstance(item, str) for item in items)
        for key, items in value.items()
    ):
        raise ValueError(f"TIER1_FILTER_CONFIG_JSON.{name} must map strings to string arrays")
    return value


def _int_setting(name: str, default: int, minimum: int = 1, config_name: str | None = None) -> int:
    raw = TIER1_FILTER_CONFIG.get(config_name or name, os.getenv(name, str(default)))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{name} must be a boolean")


TIER1_TOURNAMENT_PATTERNS = _list_setting("tournament_patterns", DEFAULT_TIER1_TOURNAMENT_PATTERNS)
FEATURED_TIER2_TOURNAMENT_PATTERNS = _list_setting(
    "featured_tier2_tournament_patterns",
    DEFAULT_FEATURED_TIER2_TOURNAMENT_PATTERNS,
)
ONLINE_LOCATION_MARKERS = _list_setting("online_location_markers", DEFAULT_ONLINE_LOCATION_MARKERS)
TRUSTED_LAN_TOURNAMENT_PATTERNS = _list_setting(
    "trusted_lan_tournament_patterns",
    DEFAULT_TRUSTED_LAN_TOURNAMENT_PATTERNS,
)
TRUSTED_LAN_TOURNAMENT_PHASE_PATTERNS = _dict_list_setting(
    "trusted_lan_tournament_phase_patterns",
    DEFAULT_TRUSTED_LAN_TOURNAMENT_PHASE_PATTERNS,
)
TRUSTED_ONLINE_TIER1_TOURNAMENT_PHASE_PATTERNS = _dict_list_setting(
    "trusted_online_tier1_tournament_phase_patterns",
    DEFAULT_TRUSTED_ONLINE_TIER1_TOURNAMENT_PHASE_PATTERNS,
)
TOURNAMENT_EXCLUSION_PATTERNS = _list_setting(
    "tournament_exclusion_patterns",
    DEFAULT_TOURNAMENT_EXCLUSION_PATTERNS,
)
TEAM_EXCLUSION_PATTERNS = _list_setting(
    "team_exclusion_patterns",
    DEFAULT_TEAM_EXCLUSION_PATTERNS,
)
TEAM_ALIASES = _dict_setting("team_aliases", DEFAULT_TEAM_ALIASES)
POPULAR_TEAMS = _list_setting("popular_teams", DEFAULT_POPULAR_TEAMS)
TIER1_PRIZE_POOL_THRESHOLD_USD = _int_setting(
    "TIER1_PRIZE_POOL_THRESHOLD_USD",
    500000,
    config_name="prize_pool_threshold_usd",
)

MATCH_SOURCE = os.getenv("MATCH_SOURCE", "auto")
if MATCH_SOURCE not in {"auto", "pandascore", "liquipedia"}:
    raise ValueError("MATCH_SOURCE must be auto, pandascore, or liquipedia")

ENABLE_LIQUIPEDIA_FALLBACK = _bool_env("ENABLE_LIQUIPEDIA_FALLBACK", True)
ENABLE_LIQUIPEDIA_SHADOW = _bool_env("ENABLE_LIQUIPEDIA_SHADOW", False)
ALLOW_STALE_IN_DRY_RUN = _bool_env("ALLOW_STALE_IN_DRY_RUN", True)
REQUEST_TIMEOUT_SECONDS = _int_setting("REQUEST_TIMEOUT_SECONDS", 15)
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow")
MAX_SOURCE_STALENESS_HOURS = _int_setting("MAX_SOURCE_STALENESS_HOURS", 48)
MAX_SOURCE_FUTURE_SKEW_HOURS = _int_setting("MAX_SOURCE_FUTURE_SKEW_HOURS", 6)
DELIVERY_CLAIM_TTL_SECONDS = _int_setting("DELIVERY_CLAIM_TTL_SECONDS", 300, minimum=30)
ALERT_COOLDOWN_SECONDS = _int_setting("ALERT_COOLDOWN_SECONDS", 21600, minimum=300)
MAX_SOURCE_RESPONSE_BYTES = _int_setting("MAX_SOURCE_RESPONSE_BYTES", 5_000_000, minimum=100_000)

OBJECT_STORAGE_BUCKET = os.getenv("OBJECT_STORAGE_BUCKET")
OBJECT_STORAGE_ENDPOINT = os.getenv("OBJECT_STORAGE_ENDPOINT", "https://storage.yandexcloud.net")

PANDASCORE_API_TOKEN = os.getenv("PANDASCORE_API_TOKEN")
PANDASCORE_API_BASE_URL = os.getenv("PANDASCORE_API_BASE_URL", "https://api.pandascore.co")
LIQUIPEDIA_API_KEY = os.getenv("LIQUIPEDIA_API_KEY") or os.getenv("LPDB_API_KEY")
LIQUIPEDIA_API_BASE_URL = os.getenv("LIQUIPEDIA_API_BASE_URL", "https://api.liquipedia.net/api/v3")
LIQUIPEDIA_WIKI = os.getenv("LIQUIPEDIA_WIKI", "counterstrike")

# Legacy sources remain in the repository only for migration tests and diagnostics.
# Production source selection intentionally never calls them.
HLTV_RESULTS_URL = "https://www.hltv.org/results"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
