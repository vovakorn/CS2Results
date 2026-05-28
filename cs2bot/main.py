"""Entry point for Yandex Cloud Functions."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Iterable, List, Sequence

import requests

from .config import BOT_MODE, CHANNELS, TELEGRAM_TOKEN
from .logging_utils import log_event
from .match_sources.match_fetcher import SourceName, get_new_finished_matches
from .match_sources.models import MatchNormalized
from .match_sources.storage import channel_match_uid, is_processed, mark_channel_processed

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
TELEGRAM_METHOD = "sendMessage"

MIN_MATCHES = 1
MAX_MATCHES = 30


def send_to_telegram(chat_id: str, text: str, timeout: int = 7) -> Dict[str, Any]:
    """Send text to ``chat_id`` via Telegram Bot API."""
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not configured")

    url = f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/{TELEGRAM_METHOD}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        # Если захочешь HTML-разметку — добавь parse_mode="HTML"
        # "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _get_attr(obj: Any, key: str, default: str = "") -> str:
    """Helper that works both with dicts and simple objects."""
    # вариант для dataclass / объекта
    if hasattr(obj, key):
        value = getattr(obj, key)
        if value is not None:
            return str(value)

    # вариант для dict, который мы получаем из HLTV
    if isinstance(obj, dict):
        value = obj.get(key)
        if value is not None:
            return str(value)

    return default


def format_match(match: Any) -> str:
    """Convert a normalized match result into a multi-line Telegram message."""
    team1 = _get_attr(match, "team1_name") or _get_attr(match, "team1", "Team 1")
    team2 = _get_attr(match, "team2_name") or _get_attr(match, "team2", "Team 2")
    score1 = _get_attr(match, "score1")
    score2 = _get_attr(match, "score2")
    event = _get_attr(match, "tournament_name") or _get_attr(match, "event")
    time = _get_attr(match, "date") or _get_attr(match, "time")
    match_id = _get_attr(match, "match_id")
    match_url = _get_attr(match, "match_url")
    source = _get_attr(match, "source")
    filter_reason = _get_attr(match, "filter_reason")
    location = _get_attr(match, "location")
    maps = getattr(match, "maps", None)

    pieces: List[str] = [f"{team1} vs {team2}"]
    if score1 != "" and score2 != "":
        pieces.append(f"Score: {score1}:{score2}")
    if event:
        pieces.append(f"Event: {event}")
    if time:
        pieces.append(f"Date: {time}")
    if location:
        pieces.append(f"Location: {location}")
    if maps:
        map_lines = []
        for item in maps:
            name = _get_attr(item, "name")
            map_score1 = _get_attr(item, "score1")
            map_score2 = _get_attr(item, "score2")
            if name and map_score1 != "" and map_score2 != "":
                map_lines.append(f"{name} {map_score1}:{map_score2}")
            elif name:
                map_lines.append(name)
        if map_lines:
            pieces.append("Maps: " + ", ".join(map_lines))
    if match_id:
        pieces.append(f"Match ID: {match_id}")
    if source:
        pieces.append(f"Source: {source}")
    if filter_reason:
        pieces.append(f"Filter: {filter_reason}")
    if match_url:
        pieces.append(match_url)

    return "\n".join(pieces)


def _match_matches_channel(match: Any, teams: Sequence[str] | None) -> bool:
    """Return True if match should be sent to channel configured with ``teams``."""
    if not teams:
        # канал без фильтра — получит все матчи
        return True

    team1 = (_get_attr(match, "team1_name") or _get_attr(match, "team1")).lower()
    team2 = (_get_attr(match, "team2_name") or _get_attr(match, "team2")).lower()
    match_teams = {team1, team2}

    for team in teams:
        if team and team.lower() in match_teams:
            return True

    return False


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y"}
    return default


def _parse_source(value: Any) -> SourceName:
    if value in {"auto", "cs2api", "hltv"}:
        return value
    return "auto"


def _parse_mode(value: Any) -> str:
    if value in {"production", "debug"}:
        return value
    return BOT_MODE if BOT_MODE in {"production", "debug"} else "production"


def _iter_channels() -> Iterable[Dict[str, Any]]:
    """Yield only channels that have a configured chat_id."""
    for channel in CHANNELS:
        chat_id = channel.get("chat_id")
        if not chat_id:
            logger.warning("Skipping channel without chat_id: %s", channel)
            continue
        yield channel


def handler(event: Dict[str, Any] | None, context: Any) -> Dict[str, Any]:
    """Yandex Cloud Functions entry point."""

    # по умолчанию берём максимум
    limit = MAX_MATCHES
    source: SourceName = "auto"
    mode = _parse_mode(None)
    dry_run = False
    include_filtered = mode == "debug"
    if isinstance(event, dict):
        requested = event.get("limit")
        if isinstance(requested, int):
            limit = max(MIN_MATCHES, min(MAX_MATCHES, requested))
        source = _parse_source(event.get("source"))
        mode = _parse_mode(event.get("mode"))
        dry_run = _parse_bool(event.get("dry_run"), default=False)
        include_filtered = _parse_bool(event.get("include_filtered"), default=mode == "debug")

    try:
        log_event(logger, logging.INFO, "handler_start", limit=limit, source=source, mode=mode, dry_run=dry_run)
        matches = asyncio.run(
            get_new_finished_matches(
                limit=limit,
                source=source,
                dry_run=dry_run,
                include_filtered=include_filtered,
                check_processed=False,
            )
        )
    except Exception as exc:
        log_event(logger, logging.ERROR, "fetch_failed", error=str(exc))
        return {
            "statusCode": 502,
            "body": json.dumps({"error": str(exc)}),
        }

    channel_stats: Dict[str, int] = {}
    skipped_duplicates = 0
    skipped_filtered = 0
    sent_messages = 0

    try:
        for match in matches:
            if not include_filtered and isinstance(match, MatchNormalized) and not match.is_tier1_lan:
                skipped_filtered += 1
                continue

            delivered = 0
            for channel in _iter_channels():
                name = channel.get("name", "unknown")
                teams = channel.get("teams")
                if not _match_matches_channel(match, teams):
                    channel_stats.setdefault(name, 0)
                    continue

                if not dry_run and isinstance(match, MatchNormalized):
                    uid = channel_match_uid(match, name)
                    if asyncio.run(is_processed(uid)):
                        skipped_duplicates += 1
                        channel_stats.setdefault(name, 0)
                        log_event(logger, logging.INFO, "duplicate_skipped", match_uid=uid, channel=name)
                        continue

                channel_stats[name] = channel_stats.get(name, 0) + 1
                if dry_run:
                    delivered += 1
                    sent_messages += 1
                    continue

                text = format_match(match)
                send_to_telegram(channel["chat_id"], text)
                delivered += 1
                sent_messages += 1

            if delivered and not dry_run and isinstance(match, MatchNormalized):
                for channel in _iter_channels():
                    name = channel.get("name", "unknown")
                    teams = channel.get("teams")
                    if _match_matches_channel(match, teams):
                        asyncio.run(mark_channel_processed(match, name))
    except Exception as exc:
        log_event(logger, logging.ERROR, "publish_failed", error=str(exc))
        return {
            "statusCode": 502,
            "body": json.dumps({"error": str(exc)}),
        }

    metrics = {
        "matches_received": len(matches),
        "messages_sent": sent_messages,
        "duplicates_skipped": skipped_duplicates,
        "filtered_skipped": skipped_filtered,
        "channels": channel_stats,
    }
    log_event(logger, logging.INFO, "handler_complete", **metrics)
    body = {
        "requested_limit": limit,
        "matches_received": len(matches),
        "messages_sent": sent_messages,
        "per_channel": channel_stats,
        "duplicates_skipped": skipped_duplicates,
        "filtered_skipped": skipped_filtered,
        "metrics": metrics,
        "dry_run": dry_run,
        "mode": mode,
        "source": source,
    }
    return {
        "statusCode": 200,
        "body": json.dumps(body),
    }
