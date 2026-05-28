from __future__ import annotations

from ..models import MatchNormalized, SourceUnavailableError


async def fetch_finished_matches(limit: int = 30) -> list[MatchNormalized]:
    raise SourceUnavailableError(
        "Browser fallback is intentionally not bundled into the Cloud Functions MVP. "
        "Run it as a separate container service if HTTP HLTV fallback becomes insufficient."
    )
