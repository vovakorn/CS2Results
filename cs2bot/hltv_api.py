"""Deprecated legacy HLTV API adapter.

New code should use ``cs2bot.match_sources``. This module is kept only for
backward compatibility with older experiments.
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

HLTV_RESULTS_URL = "https://hltv-api.vercel.app/api/results"
DEFAULT_LIMIT = 3


def fetch_results(limit: int = DEFAULT_LIMIT, timeout: int = 10) -> List[Dict[str, Any]]:
    """Return the first ``limit`` match results from the HLTV API as dicts.

    :param limit: maximum number of matches to return
    :param timeout: HTTP timeout in seconds
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")

    response = requests.get(HLTV_RESULTS_URL, timeout=timeout)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        # Для v1 просто считаем это ошибкой формата данных
        raise ValueError("Unexpected HLTV API response format")

    return data[:limit]
