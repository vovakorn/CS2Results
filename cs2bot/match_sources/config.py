from __future__ import annotations

import json
import os


DEFAULT_KNOWN_TIER1_OPERATORS = [
    "ESL",
    "IEM",
    "PGL",
    "BLAST",
    "Esports World Cup",
    "FISSURE",
]

DEFAULT_TIER1_TOURNAMENT_PATTERNS = [
    "IEM",
    "ESL Pro League",
    "PGL",
    "BLAST Open",
    "BLAST Rivals",
    "Esports World Cup",
    "FISSURE Playground",
    "CS Asia Championships",
    "Major",
]

DEFAULT_ONLINE_LOCATION_MARKERS = ["online", "remote"]


def _load_json_config(env_name: str) -> dict:
    raw = os.getenv(env_name)
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{env_name} must be a JSON object")
    return data


TIER1_FILTER_CONFIG = _load_json_config("TIER1_FILTER_CONFIG_JSON")


def _list_setting(name: str, default: list[str]) -> list[str]:
    value = TIER1_FILTER_CONFIG.get(name, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"TIER1_FILTER_CONFIG_JSON.{name} must be a string array")
    return value


KNOWN_TIER1_OPERATORS = _list_setting("known_operators", DEFAULT_KNOWN_TIER1_OPERATORS)
TIER1_TOURNAMENT_PATTERNS = _list_setting("tournament_patterns", DEFAULT_TIER1_TOURNAMENT_PATTERNS)
ONLINE_LOCATION_MARKERS = _list_setting("online_location_markers", DEFAULT_ONLINE_LOCATION_MARKERS)
TIER1_PRIZE_POOL_THRESHOLD_USD = int(
    TIER1_FILTER_CONFIG.get("prize_pool_threshold_usd", os.getenv("TIER1_PRIZE_POOL_THRESHOLD_USD", "500000"))
)

MATCH_SOURCE = os.getenv("MATCH_SOURCE", "auto")
ENABLE_HLTV_FALLBACK = os.getenv("ENABLE_HLTV_FALLBACK", "1") not in {"0", "false", "False"}
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Berlin")

OBJECT_STORAGE_BUCKET = os.getenv("OBJECT_STORAGE_BUCKET")
OBJECT_STORAGE_ENDPOINT = os.getenv("OBJECT_STORAGE_ENDPOINT", "https://storage.yandexcloud.net")

HLTV_RESULTS_URL = "https://www.hltv.org/results"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
