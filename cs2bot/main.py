"""Entry point for Yandex Cloud Functions."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .config import (
    BOT_MODE,
    CHANNELS,
    TELEGRAM_ADMIN_CHAT_ID,
    TELEGRAM_SPOILERS,
    TELEGRAM_TOKEN,
)
from .logging_utils import log_event
from .match_sources.config import (
    DISPLAY_TIMEZONE,
    ENABLE_LIQUIPEDIA_FALLBACK,
    LIQUIPEDIA_API_KEY,
    MATCH_SOURCE,
    OBJECT_STORAGE_BUCKET,
    PANDASCORE_API_TOKEN,
)
from .match_sources.match_fetcher import SourceName, get_new_finished_matches
from .match_sources.models import MatchNormalized
from .match_sources.storage import (
    claim_admin_alert,
    claim_channel_delivery,
    mark_channel_processed,
    release_delivery_claim,
)

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
TELEGRAM_METHOD = "sendMessage"

MIN_MATCHES = 1
MAX_MATCHES = 30
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MATCH_URL_HOSTS = {
    "pandascore": {"pandascore.co", "www.pandascore.co"},
    "liquipedia": {"liquipedia.net", "www.liquipedia.net"},
    "cs2api": {"bo3.gg", "www.bo3.gg"},
    "hltv": {"hltv.org", "www.hltv.org"},
}
SOURCE_LABELS = {
    "pandascore": "PandaScore",
    "liquipedia": "Liquipedia",
    "cs2api": "BO3.gg",
    "hltv": "HLTV",
}
RUSSIAN_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class TelegramDeliveryError(RuntimeError):
    """A safe Telegram failure that never contains the bot token or request URL."""


def _notify_admin(alert_code: str, message: str) -> None:
    """Send a rate-limited operational alert without affecting public delivery."""
    if not TELEGRAM_ADMIN_CHAT_ID or not TELEGRAM_TOKEN:
        log_event(logger, logging.WARNING, "admin_alert_not_configured", alert_code=alert_code)
        return

    try:
        if OBJECT_STORAGE_BUCKET and not asyncio.run(claim_admin_alert(alert_code)):
            log_event(logger, logging.INFO, "admin_alert_suppressed", alert_code=alert_code)
            return
        send_to_telegram(
            TELEGRAM_ADMIN_CHAT_ID,
            f"⚠️ <b>CS2 Results Bot</b>\n{html.escape(message)}",
        )
        log_event(logger, logging.INFO, "admin_alert_sent", alert_code=alert_code)
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "admin_alert_failed",
            alert_code=alert_code,
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    for secret in (TELEGRAM_TOKEN, PANDASCORE_API_TOKEN, LIQUIPEDIA_API_KEY):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"/bot[^/\s]+/", "/bot[REDACTED]/", message, flags=re.IGNORECASE)
    return message[:500] or type(exc).__name__


def send_to_telegram(
    chat_id: str | int,
    text: str,
    timeout: int = 7,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Send text to ``chat_id`` via Telegram Bot API."""
    if not TELEGRAM_TOKEN:
        raise TelegramDeliveryError("Telegram credentials are not configured")

    url = f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/{TELEGRAM_METHOD}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            raise TelegramDeliveryError("Telegram request failed") from exc

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < attempts:
                retry_after = 0
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 0))
                except (TypeError, ValueError, AttributeError, requests.JSONDecodeError):
                    retry_after = 0
                time.sleep(min(max(retry_after, 2 ** (attempt - 1)), 5))
                continue
        if response.status_code >= 300:
            raise TelegramDeliveryError(f"Telegram API returned HTTP {response.status_code}")

        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise TelegramDeliveryError("Telegram API returned invalid JSON") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise TelegramDeliveryError("Telegram API rejected the message")
        return data

    raise TelegramDeliveryError("Telegram request failed")


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


def _format_display_time(value: str) -> str:
    if not value or "T" not in value:
        return value

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value

    try:
        timezone = ZoneInfo(DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError:
        return value

    local = parsed.astimezone(timezone)
    timezone_label = "МСК" if DISPLAY_TIMEZONE == "Europe/Moscow" else local.tzname() or ""
    return (
        f"{local.day} {RUSSIAN_MONTHS[local.month]} {local.year}, "
        f"{local:%H:%M} {timezone_label}"
    ).strip()


def _safe_match_url(value: str, source: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if (parsed.hostname or "").casefold() not in MATCH_URL_HOSTS.get(source, set()):
        return ""
    return value


def format_match(match: Any) -> str:
    """Convert a normalized match result into the final public Telegram template."""
    team1 = _get_attr(match, "team1_name") or _get_attr(match, "team1", "Team 1")
    team2 = _get_attr(match, "team2_name") or _get_attr(match, "team2", "Team 2")
    score1 = _get_attr(match, "score1")
    score2 = _get_attr(match, "score2")
    event = _get_attr(match, "tournament_name") or _get_attr(match, "event")
    time = _format_display_time(
        _get_attr(match, "end_date")
        or _get_attr(match, "date")
        or _get_attr(match, "start_date")
        or _get_attr(match, "time")
    )
    match_url = _get_attr(match, "match_url")
    source = _get_attr(match, "source")
    maps = getattr(match, "maps", None)

    safe_team1 = html.escape(team1)
    safe_team2 = html.escape(team2)
    safe_event = html.escape(event)
    pieces: List[str] = []
    if event:
        pieces.append(f"🏆 <b>{safe_event}</b>")
        pieces.append("")
    pieces.append(f"⚔️ <b>{safe_team1} — {safe_team2}</b>")
    if score1 != "" and score2 != "":
        safe_score = f"{html.escape(score1)}:{html.escape(score2)}"
        try:
            winner = team1 if int(score1) > int(score2) else team2 if int(score2) > int(score1) else ""
        except ValueError:
            winner = ""
        if TELEGRAM_SPOILERS:
            safe_score = f"<tg-spoiler>{safe_score}</tg-spoiler>"
        pieces.append(f"📊 Счёт: {safe_score}")
        if winner:
            safe_winner = html.escape(winner)
            if TELEGRAM_SPOILERS:
                safe_winner = f"<tg-spoiler>{safe_winner}</tg-spoiler>"
            pieces.append(f"✅ Победитель: {safe_winner}")
    if time:
        pieces.append(f"🕒 Дата: {html.escape(time)}")
    if maps:
        map_lines = []
        for item in maps:
            name = _get_attr(item, "name")
            map_score1 = _get_attr(item, "score1")
            map_score2 = _get_attr(item, "score2")
            if name and map_score1 != "" and map_score2 != "":
                map_lines.append(
                    f"{html.escape(name)} {html.escape(map_score1)}:{html.escape(map_score2)}"
                )
            elif name:
                map_lines.append(html.escape(name))
        if map_lines:
            pieces.append("🗺 Карты: " + ", ".join(map_lines))
    if source:
        source_label = html.escape(SOURCE_LABELS.get(source, source))
        safe_url = _safe_match_url(match_url, source)
        if safe_url:
            pieces.append(f'Источник: <a href="{html.escape(safe_url, quote=True)}">{source_label}</a>')
        else:
            pieces.append(f"Источник: {source_label}")
    pieces.extend(["", "#CS2 #РезультатыМатчей"])

    message = "\n".join(pieces)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        return message[: MAX_TELEGRAM_MESSAGE_LENGTH - 1] + "…"
    return message


def _match_matches_channel(match: Any, teams: Sequence[str] | None) -> bool:
    """Return True if match should be sent to channel configured with ``teams``."""
    if not teams:
        # канал без фильтра — получит все матчи
        return True

    team1 = (_get_attr(match, "team1_name") or _get_attr(match, "team1")).strip().casefold()
    team2 = (_get_attr(match, "team2_name") or _get_attr(match, "team2")).strip().casefold()
    match_teams = {team1, team2}

    for team in teams:
        if team and team.strip().casefold() in match_teams:
            return True

    return False


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    raise ValueError("boolean event fields must use true or false")


def _parse_source(value: Any, default: str | None = None) -> SourceName:
    if value is None:
        value = default or MATCH_SOURCE
    if value in {"auto", "pandascore", "liquipedia"}:
        return value
    raise ValueError("source must be auto, pandascore, or liquipedia")


def _parse_mode(value: Any) -> str:
    if value is None:
        return BOT_MODE if BOT_MODE in {"production", "debug"} else "production"
    if value in {"production", "debug"}:
        return value
    raise ValueError("mode must be production or debug")


def _iter_channels() -> Iterable[Dict[str, Any]]:
    """Yield only channels that have a configured chat_id."""
    for channel in CHANNELS:
        chat_id = channel.get("chat_id")
        if not chat_id:
            logger.warning("Skipping channel without chat_id: %s", channel)
            continue
        yield channel


def _error_response(status_code: int, code: str) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "body": json.dumps({"error": code}),
    }


def handler(event: Dict[str, Any] | None, context: Any) -> Dict[str, Any]:
    """Yandex Cloud Functions entry point."""

    # по умолчанию берём максимум
    limit = MAX_MATCHES
    source: SourceName = _parse_source(None)
    mode = _parse_mode(None)
    dry_run = False
    include_filtered = False
    try:
        if isinstance(event, dict):
            requested = event.get("limit")
            if isinstance(requested, int) and not isinstance(requested, bool):
                limit = max(MIN_MATCHES, min(MAX_MATCHES, requested))
            source = _parse_source(event.get("source"))
            mode = _parse_mode(event.get("mode"))
            dry_run = _parse_bool(event.get("dry_run"), default=False)
            requested_filtered = _parse_bool(event.get("include_filtered"), default=mode == "debug")
            include_filtered = requested_filtered and dry_run
            if requested_filtered and not dry_run:
                log_event(
                    logger,
                    logging.WARNING,
                    "unsafe_filtered_publication_blocked",
                    mode=mode,
                )
    except ValueError as exc:
        log_event(logger, logging.WARNING, "invalid_request", error=_safe_error_message(exc))
        return _error_response(400, "invalid_request")

    if not dry_run:
        missing_config = []
        if not TELEGRAM_TOKEN:
            missing_config.append("telegram_credentials")
        if not OBJECT_STORAGE_BUCKET:
            missing_config.append("object_storage_bucket")
        if not any(True for _ in _iter_channels()):
            missing_config.append("delivery_channels")
        if source == "pandascore" and not PANDASCORE_API_TOKEN:
            missing_config.append("match_source_credentials")
        elif source == "liquipedia" and not LIQUIPEDIA_API_KEY:
            missing_config.append("match_source_credentials")
        elif source == "auto" and not PANDASCORE_API_TOKEN and not (
            ENABLE_LIQUIPEDIA_FALLBACK and LIQUIPEDIA_API_KEY
        ):
            missing_config.append("match_source_credentials")
        if missing_config:
            log_event(
                logger,
                logging.ERROR,
                "configuration_invalid",
                missing=missing_config,
            )
            _notify_admin("configuration_invalid", "Конфигурация функции неполна.")
            return _error_response(503, "configuration_error")

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
        log_event(
            logger,
            logging.ERROR,
            "fetch_failed",
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )
        _notify_admin(
            "match_source_unavailable",
            "Источник матчей недоступен, пуст или не обновлялся более 48 часов.",
        )
        return _error_response(502, "match_source_unavailable")

    channel_stats: Dict[str, int] = {}
    skipped_duplicates = 0
    skipped_filtered = 0
    sent_messages = 0
    failed_messages = 0

    for match in matches:
        if isinstance(match, MatchNormalized) and not match.is_tier1_lan and not include_filtered:
            skipped_filtered += 1
            continue

        for channel in _iter_channels():
            name = str(channel.get("name", "unknown"))
            channel_id = str(channel.get("id") or name)
            teams = channel.get("teams")
            channel_stats.setdefault(name, 0)
            if not _match_matches_channel(match, teams):
                continue

            if dry_run:
                channel_stats[name] += 1
                sent_messages += 1
                continue

            if not isinstance(match, MatchNormalized):
                skipped_filtered += 1
                continue

            try:
                claim = asyncio.run(
                    claim_channel_delivery(
                        match,
                        channel_id,
                        legacy_channel_name=name,
                    )
                )
            except Exception as exc:
                failed_messages += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "delivery_claim_failed",
                    channel=name,
                    match_uid=match.match_uid,
                    error_type=type(exc).__name__,
                    error=_safe_error_message(exc),
                )
                continue

            if claim is None:
                skipped_duplicates += 1
                log_event(
                    logger,
                    logging.INFO,
                    "duplicate_or_inflight_skipped",
                    match_uid=match.match_uid,
                    channel=name,
                )
                continue

            try:
                text = format_match(match)
                send_to_telegram(channel["chat_id"], text)
                channel_stats[name] += 1
                sent_messages += 1
            except Exception as exc:
                failed_messages += 1
                try:
                    asyncio.run(release_delivery_claim(claim))
                except Exception as release_exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "delivery_claim_release_failed",
                        channel=name,
                        match_uid=match.match_uid,
                        error_type=type(release_exc).__name__,
                    )
                log_event(
                    logger,
                    logging.ERROR,
                    "publish_failed",
                    channel=name,
                    match_uid=match.match_uid,
                    error_type=type(exc).__name__,
                    error=_safe_error_message(exc),
                )
                continue

            try:
                asyncio.run(mark_channel_processed(match, channel_id))
            except Exception as exc:
                failed_messages += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "delivery_state_failed",
                    channel=name,
                    match_uid=match.match_uid,
                    error_type=type(exc).__name__,
                    error=_safe_error_message(exc),
                )

    metrics = {
        "matches_received": len(matches),
        "messages_sent": sent_messages,
        "duplicates_skipped": skipped_duplicates,
        "filtered_skipped": skipped_filtered,
        "delivery_failures": failed_messages,
        "channels": channel_stats,
    }
    log_event(logger, logging.INFO, "handler_complete", **metrics)
    if failed_messages:
        _notify_admin(
            "delivery_failed",
            f"Не доставлено сообщений: {failed_messages}. Проверьте логи функции.",
        )
    body = {
        "requested_limit": limit,
        "matches_received": len(matches),
        "messages_sent": sent_messages,
        "per_channel": channel_stats,
        "duplicates_skipped": skipped_duplicates,
        "filtered_skipped": skipped_filtered,
        "delivery_failures": failed_messages,
        "metrics": metrics,
        "dry_run": dry_run,
        "mode": mode,
        "source": source,
    }
    return {
        "statusCode": 502 if failed_messages else 200,
        "body": json.dumps(body),
    }
