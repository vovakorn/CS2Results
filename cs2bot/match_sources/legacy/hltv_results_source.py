from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup, Tag

from ..config import (
    DEFAULT_USER_AGENT,
    HLTV_RESULTS_URL,
    MAX_SOURCE_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
)
from ..models import MatchDetails, MatchNormalized, SourceUnavailableError
from ..sources.http_utils import read_limited_response

logger = logging.getLogger(__name__)


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _extract_match_id(match_url: str | None) -> str | None:
    if not match_url:
        return None
    match = re.search(r"/matches/(\d+)", match_url)
    return match.group(1) if match else None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _normalize_date_from_unix(value: str | None) -> str | None:
    if not value:
        return None
    try:
        timestamp = int(value)
        if timestamp > 9_999_999_999:
            timestamp = timestamp // 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


async def fetch_html(url: str = HLTV_RESULTS_URL) -> str:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status >= 300:
                    raise SourceUnavailableError(f"HLTV returned HTTP {response.status}")
                raw = await read_limited_response(response, MAX_SOURCE_RESPONSE_BYTES, "HLTV")
                return raw.decode(response.charset or "utf-8", errors="replace")
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"HLTV request failed: {exc}") from exc


async def fetch_match_details(match_url: str) -> MatchDetails:
    """Placeholder for later detail-page enrichment; MVP does not require it."""

    logger.debug("Skipping HLTV match details fetch in MVP url=%s", match_url)
    return MatchDetails()


def _find_date_for_node(node: Tag) -> str | None:
    current: Tag | None = node
    while current:
        unix = current.get("data-zonedgrouping-entry-unix")
        parsed = _normalize_date_from_unix(unix if isinstance(unix, str) else None)
        if parsed:
            return parsed
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def _parse_result_node(node: Tag) -> MatchNormalized:
    link_tag = node if node.name == "a" else node.find("a", href=re.compile(r"/matches/\d+"))
    href = link_tag.get("href") if isinstance(link_tag, Tag) else None
    match_url = urljoin("https://www.hltv.org", str(href)) if href else None
    match_id = _extract_match_id(match_url)

    team_tags = node.select(".team, .team1 .team, .team2 .team, .team-cell")
    teams = [_clean_text(tag.get_text(" ")) for tag in team_tags]
    teams = [team for team in teams if team]
    if len(teams) < 2:
        team_name_tags = node.select("[class*=team]")
        teams = [_clean_text(tag.get_text(" ")) for tag in team_name_tags if _clean_text(tag.get_text(" "))]

    score_tags = node.select(".result-score, .score, .result .score")
    scores = [_parse_int(tag.get_text(" ")) for tag in score_tags]
    scores = [score for score in scores if score is not None]
    if len(scores) < 2:
        text = _clean_text(node.get_text(" "))
        score_match = re.search(r"(\d+)\s*[-:]\s*(\d+)", text)
        if score_match:
            scores = [int(score_match.group(1)), int(score_match.group(2))]

    event_tag = node.select_one(".event-name, .event .event-name, .event, [class*=event-name]")
    tournament_name = _clean_text(event_tag.get_text(" ")) if event_tag else ""

    if len(teams) < 2:
        raise ValueError("missing team name")
    if len(scores) < 2:
        raise ValueError("missing score")
    if not tournament_name:
        raise ValueError("missing tournament")
    if not match_id and not match_url:
        raise ValueError("missing stable identifier")

    return MatchNormalized(
        source="hltv",
        match_id=match_id,
        match_url=match_url,
        tournament_name=tournament_name,
        team1_name=teams[0],
        team2_name=teams[1],
        score1=scores[0],
        score2=scores[1],
        maps=[],
        date=_find_date_for_node(node),
    )


def parse_results_page(html: str, limit: int = 30) -> list[MatchNormalized]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[Tag] = []

    for selector in ["a.result-con", ".result-con", ".results-all a[href*='/matches/']"]:
        for node in soup.select(selector):
            if isinstance(node, Tag) and node not in candidates:
                candidates.append(node)

    if not candidates:
        for link in soup.find_all("a", href=re.compile(r"/matches/\d+")):
            if isinstance(link, Tag):
                candidates.append(link)
    if not candidates:
        raise SourceUnavailableError("HLTV results page contains no recognizable match nodes")

    matches: list[MatchNormalized] = []
    for node in candidates:
        if len(matches) >= limit:
            break
        try:
            matches.append(_parse_result_node(node))
        except Exception as exc:
            href_tag = node if node.name == "a" else node.find("a", href=True)
            url = href_tag.get("href") if isinstance(href_tag, Tag) else None
            logger.error('source=hltv parser_error="%s" url="%s"', exc, url)
            continue
    if not matches:
        raise SourceUnavailableError("HLTV result nodes could not be normalized")
    return matches


async def fetch_finished_matches(limit: int = 30) -> list[MatchNormalized]:
    html = await fetch_html(HLTV_RESULTS_URL)
    return parse_results_page(html, limit=limit)
