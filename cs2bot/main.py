"""Entry point for Yandex Cloud Functions."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, time as datetime_time, timezone
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .config import (
    BOT_MODE,
    CHANNELS,
    TELEGRAM_ADMIN_CHAT_ID,
    TELEGRAM_MEDIA_CARDS,
    TELEGRAM_SPOILERS,
    TELEGRAM_TOKEN,
)
from .logging_utils import log_event
from .media_cards import (
    MAX_RESULT_MATCHES,
    MAX_SCHEDULE_MATCHES,
    render_result_card,
    render_results_card,
    render_schedule_card,
)
from .match_sources.config import (
    DISPLAY_TIMEZONE,
    ENABLE_LIQUIPEDIA_FALLBACK,
    LIQUIPEDIA_API_KEY,
    MATCH_SOURCE,
    OBJECT_STORAGE_BUCKET,
    PANDASCORE_API_TOKEN,
)
from .match_sources.filters import is_tier1_candidate
from .match_sources.match_fetcher import SourceName, apply_quality_filters, get_new_finished_matches
from .match_sources.models import MatchNormalized, UpcomingMatchNormalized
from .match_sources.sources.pandascore_source import (
    fetch_finished_matches as fetch_pandascore_finished_matches,
    fetch_upcoming_matches,
)
from .match_sources.storage import (
    claim_admin_alert,
    claim_channel_delivery,
    claim_content_delivery,
    mark_channel_processed,
    mark_content_processed,
    release_delivery_claim,
)

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
TELEGRAM_METHOD = "sendMessage"

MIN_MATCHES = 1
MAX_MATCHES = 30
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MAX_TELEGRAM_CAPTION_LENGTH = 1024
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
CONTENT_JOBS = {"results", "schedule", "digest"}
TEST_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class TelegramDeliveryError(RuntimeError):
    """A safe Telegram failure that never contains the bot token or request URL."""


def _match_diagnostic(match: MatchNormalized) -> Dict[str, Any]:
    """Return public source fields that are safe to expose in dry-run output."""
    return {
        "source": match.source,
        "match_id": match.match_id,
        "tournament": match.tournament_name,
        "competition_key": match.competition_key,
        "source_refs": match.source_refs.model_dump() if match.source_refs else None,
        "tournament_tier": match.tournament_tier,
        "teams": [match.team1_name, match.team2_name],
        "score": [match.score1, match.score2],
        "date": match.date,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "original_scheduled_at": match.original_scheduled_at,
        "rescheduled": match.rescheduled,
        "forfeit": match.forfeit,
        "is_lan": match.is_lan,
        "location": match.location,
        "is_tier1_lan": match.is_tier1_lan,
        "filter_reason": match.filter_reason,
    }


def _upcoming_diagnostic(match: UpcomingMatchNormalized) -> Dict[str, Any]:
    """Return public schedule fields without exposing full remote image URLs."""
    return {
        "match_id": match.match_id,
        "tournament": match.tournament_name,
        "competition_key": match.competition_key,
        "source_refs": match.source_refs.model_dump() if match.source_refs else None,
        "tournament_tier": match.tournament_tier,
        "teams": [match.team1_name, match.team2_name],
        "team_logos_present": [
            bool(match.team1_logo_url or match.team1_logo_fallback_url),
            bool(match.team2_logo_url or match.team2_logo_fallback_url),
        ],
        "scheduled_at": match.scheduled_at,
        "original_scheduled_at": match.original_scheduled_at,
        "rescheduled": match.rescheduled,
        "forfeit": match.forfeit,
        "is_featured": match.is_featured,
        "feature_reason": match.feature_reason,
    }


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


def send_photo_to_telegram(
    chat_id: str | int,
    photo: bytes,
    caption: str,
    *,
    has_spoiler: bool = False,
    filename: str = "cs2-match.png",
    timeout: int = 10,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Send an in-memory PNG through Telegram without exposing credentials."""
    if not TELEGRAM_TOKEN:
        raise TelegramDeliveryError("Telegram credentials are not configured")
    if not photo:
        raise TelegramDeliveryError("Telegram photo is empty")
    if len(caption) > MAX_TELEGRAM_CAPTION_LENGTH:
        raise TelegramDeliveryError("Telegram photo caption is too long")

    url = f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
        "has_spoiler": "true" if has_spoiler else "false",
    }
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                data=payload,
                files={"photo": (filename, photo, "image/png")},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            raise TelegramDeliveryError("Telegram photo request failed") from exc

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
            raise TelegramDeliveryError("Telegram API rejected the photo")
        return data

    raise TelegramDeliveryError("Telegram photo request failed")


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
    match_url = _get_attr(match, "match_url")
    source = _get_attr(match, "source")
    maps = getattr(match, "maps", None)

    safe_team1 = html.escape(team1.upper())
    safe_team2 = html.escape(team2.upper())
    safe_event = html.escape(event.upper())
    pieces: List[str] = []
    if event:
        pieces.append(f"<b>{safe_event}</b>")
        pieces.append("")
    if score1 != "" and score2 != "":
        safe_score = f"{html.escape(score1)} : {html.escape(score2)}"
        try:
            winner = team1 if int(score1) > int(score2) else team2 if int(score2) > int(score1) else ""
        except ValueError:
            winner = ""
        if TELEGRAM_SPOILERS:
            safe_score = f"<tg-spoiler>{safe_score}</tg-spoiler>"
        pieces.append(f"<b>{safe_team1}</b>  {safe_score}  <b>{safe_team2}</b>")
        if winner:
            safe_winner = f"<b>{html.escape(winner.upper())}</b>"
            if TELEGRAM_SPOILERS:
                safe_winner = f"<tg-spoiler>{safe_winner}</tg-spoiler>"
            pieces.append(f"Победитель: {safe_winner}")
    else:
        pieces.append(f"<b>{safe_team1}</b> — <b>{safe_team2}</b>")
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
            pieces.append("Карты — " + " · ".join(map_lines))
    if source:
        source_label = html.escape(SOURCE_LABELS.get(source, source))
        safe_url = _safe_match_url(match_url, source)
        if safe_url:
            source_label = f'<a href="{html.escape(safe_url, quote=True)}">{source_label}</a>'
        pieces.extend(["", f"{source_label} · #CS2 #РезультатыМатчей"])
    else:
        pieces.extend(["", "#CS2 #РезультатыМатчей"])

    message = "\n".join(pieces)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        return message[: MAX_TELEGRAM_MESSAGE_LENGTH - 1] + "…"
    return message


def _local_day_window(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    try:
        local_timezone = ZoneInfo(DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("DISPLAY_TIMEZONE is invalid") from exc
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    local_now = reference.astimezone(local_timezone)
    start = datetime.combine(local_now.date(), datetime_time.min, tzinfo=local_timezone)
    end = datetime.combine(local_now.date().fromordinal(local_now.date().toordinal() + 1), datetime_time.min, tzinfo=local_timezone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), local_now


def _display_day(local_now: datetime) -> str:
    return f"{local_now.day} {RUSSIAN_MONTHS[local_now.month]}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_daily_schedule(
    matches: Sequence[UpcomingMatchNormalized],
    local_now: datetime,
) -> str:
    """Build one compact Moscow-time schedule message."""
    timezone_info = ZoneInfo(DISPLAY_TIMEZONE)
    header = [
        f"📅 <b>Матчи CS2 сегодня — {_display_day(local_now)}</b>",
        "",
    ]
    entries: list[list[str]] = []
    sorted_matches = sorted(
        matches,
        key=lambda item: _parse_datetime(item.scheduled_at) or datetime.max.replace(tzinfo=timezone.utc),
    )
    for match in sorted_matches:
        parsed = _parse_datetime(match.scheduled_at)
        local_time = parsed.astimezone(timezone_info).strftime("%H:%M") if parsed else "—"
        entries.append(
            [
                f"🕙 {local_time} — <b>{html.escape(match.team1_name)} — {html.escape(match.team2_name)}</b>",
                f"🏆 {html.escape(match.tournament_name)}",
                "",
            ]
        )
    footer = ["Источник: PandaScore", "", "#CS2 #РасписаниеМатчей"]
    lines = list(header)
    omitted = 0
    for index, entry in enumerate(entries):
        remaining = len(entries) - index - 1
        suffix = ([f"… и ещё {remaining + 1} матчей", ""] if remaining >= 0 else []) + footer
        if len("\n".join(lines + entry + suffix).strip()) > MAX_TELEGRAM_MESSAGE_LENGTH:
            omitted = remaining + 1
            break
        lines.extend(entry)
    if omitted:
        lines.extend([f"… и ещё {omitted} матчей", ""])
    lines.extend(footer)
    return "\n".join(lines).strip()


def format_schedule_photo_caption(local_now: datetime, match_count: int) -> str:
    """Build a deliberately short caption; the detailed schedule lives on the card."""
    noun = "матч" if match_count == 1 else "матча" if 2 <= match_count <= 4 else "матчей"
    return "\n".join(
        [
            f"📅 <b>Матчи CS2 сегодня — {_display_day(local_now)}</b>",
            f"{match_count} {noun}",
            "",
            "Источник: PandaScore",
            "",
            "#CS2 #РасписаниеМатчей",
        ]
    )


def format_digest_photo_caption(local_now: datetime, match_count: int) -> str:
    """Build a short caption while scores remain protected by the media spoiler."""
    noun = "результат" if match_count == 1 else "результата" if 2 <= match_count <= 4 else "результатов"
    return "\n".join(
        [
            f"🌙 <b>Итоги дня — {_display_day(local_now)}</b>",
            f"{match_count} {noun}",
            "",
            "Источник: PandaScore",
            "",
            "#CS2 #ИтогиДня",
        ]
    )


def format_daily_digest(matches: Sequence[MatchNormalized], local_now: datetime) -> str:
    """Build a short evening recap with scores hidden as spoilers."""
    header = [f"🌙 <b>Итоги дня — {_display_day(local_now)}</b>", ""]
    entries: list[list[str]] = []
    sorted_matches = sorted(
        matches,
        key=lambda item: _parse_datetime(item.end_date or item.date) or datetime.min.replace(tzinfo=timezone.utc),
    )
    for match in sorted_matches:
        score = f"{match.score1}:{match.score2}"
        if TELEGRAM_SPOILERS:
            score = f"<tg-spoiler>{score}</tg-spoiler>"
        entries.append(
            [
                f"⚔️ <b>{html.escape(match.team1_name)} — {html.escape(match.team2_name)}</b>",
                f"📊 {score} · {html.escape(match.tournament_name)}",
                "",
            ]
        )
    footer = ["Источник: PandaScore", "", "#CS2 #ИтогиДня"]
    lines = list(header)
    omitted = 0
    for index, entry in enumerate(entries):
        remaining = len(entries) - index - 1
        suffix = ([f"… и ещё {remaining + 1} матчей", ""] if remaining >= 0 else []) + footer
        if len("\n".join(lines + entry + suffix).strip()) > MAX_TELEGRAM_MESSAGE_LENGTH:
            omitted = remaining + 1
            break
        lines.extend(entry)
    if omitted:
        lines.extend([f"… и ещё {omitted} матчей", ""])
    lines.extend(footer)
    return "\n".join(lines).strip()


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


def _unwrap_timer_event(event: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return the JSON payload from a Yandex timer event, or the direct event."""
    if not isinstance(event, dict):
        return {}
    messages = event.get("messages")
    if not isinstance(messages, list) or not messages:
        return event
    first = messages[0]
    details = first.get("details") if isinstance(first, dict) else None
    payload = details.get("payload") if isinstance(details, dict) else None
    if not isinstance(payload, str):
        raise ValueError("timer payload must be a JSON string")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("timer payload must contain a JSON object")
    return decoded


def _handle_content_job(
    job: str,
    dry_run: bool,
    test_run_id: str | None = None,
) -> Dict[str, Any]:
    start, end, local_now = _local_day_window()
    try:
        if job == "schedule":
            fetched = asyncio.run(fetch_upcoming_matches(start, end))
            selected = [match for match in fetched if match.is_featured]
            text = format_daily_schedule(selected, local_now) if selected else ""
        else:
            fetched_results = asyncio.run(fetch_pandascore_finished_matches(100, start=start, end=end))
            selected_results, _, _ = apply_quality_filters(fetched_results)
            selected = [match for match in selected_results if match.is_tier1_lan]
            text = format_daily_digest(selected, local_now) if selected else ""
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "content_fetch_failed",
            job=job,
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )
        _notify_admin(f"{job}_source_unavailable", f"Не удалось подготовить выпуск «{job}».")
        return _error_response(502, "match_source_unavailable")

    if not text:
        body = {
            "job": job,
            "matches_received": len(fetched if job == "schedule" else fetched_results),
            "matches_selected": 0,
            "messages_sent": 0,
            "duplicates_skipped": 0,
            "delivery_failures": 0,
            "dry_run": dry_run,
        }
        return {"statusCode": 200, "body": json.dumps(body)}

    media_card: bytes | None = None
    media_card_error: str | None = None
    card_supported = (
        job == "schedule" and 1 <= len(selected) <= MAX_SCHEDULE_MATCHES
    ) or (
        job == "digest" and 1 <= len(selected) <= MAX_RESULT_MATCHES
    )
    if TELEGRAM_MEDIA_CARDS and card_supported:
        try:
            if job == "schedule":
                media_card = render_schedule_card(selected, local_now, DISPLAY_TIMEZONE)
            else:
                media_card = render_results_card(selected, local_now)
        except Exception as exc:
            media_card_error = type(exc).__name__
            log_event(
                logger,
                logging.WARNING,
                "media_card_fallback",
                job=job,
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )

    sent = 0
    duplicates = 0
    failures = 0
    preview = text if dry_run else None
    day_key = local_now.date().isoformat()
    for channel in _iter_channels():
        if dry_run:
            sent += 1
            continue
        channel_id = str(channel.get("id") or channel.get("name", "unknown"))
        content_uid = f"{job}_{day_key}_{channel_id}"
        if test_run_id:
            content_uid = f"{content_uid}_test_{test_run_id}"
        claim = None
        try:
            claim = asyncio.run(claim_content_delivery(content_uid))
            if claim is None:
                duplicates += 1
                continue
            if media_card is not None:
                try:
                    if job == "schedule":
                        caption = format_schedule_photo_caption(local_now, len(selected))
                        filename = f"cs2-schedule-{day_key}.png"
                        has_spoiler = False
                    else:
                        caption = format_digest_photo_caption(local_now, len(selected))
                        filename = f"cs2-results-{day_key}.png"
                        has_spoiler = TELEGRAM_SPOILERS
                    if test_run_id:
                        caption = f"🧪 <b>Тестовая карточка</b>\n\n{caption}"
                    send_photo_to_telegram(
                        channel["chat_id"],
                        media_card,
                        caption,
                        has_spoiler=has_spoiler,
                        filename=filename,
                    )
                except Exception as exc:
                    media_card_error = type(exc).__name__
                    log_event(
                        logger,
                        logging.WARNING,
                        "media_card_delivery_fallback",
                        job=job,
                        channel=channel_id,
                        error_type=type(exc).__name__,
                        error=_safe_error_message(exc),
                    )
                    send_to_telegram(channel["chat_id"], text)
            else:
                send_to_telegram(channel["chat_id"], text)
            asyncio.run(mark_content_processed(content_uid, job))
            sent += 1
        except Exception as exc:
            failures += 1
            if claim is not None:
                try:
                    asyncio.run(release_delivery_claim(claim))
                except Exception:
                    pass
            log_event(
                logger,
                logging.ERROR,
                "content_publish_failed",
                job=job,
                channel=channel_id,
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )

    if failures:
        _notify_admin("delivery_failed", f"Не доставлен выпуск «{job}»: ошибок {failures}.")
    body = {
        "job": job,
        "matches_received": len(fetched if job == "schedule" else fetched_results),
        "matches_selected": len(selected),
        "messages_sent": sent,
        "duplicates_skipped": duplicates,
        "delivery_failures": failures,
        "dry_run": dry_run,
    }
    if test_run_id:
        body["test_run_id"] = test_run_id
    if preview is not None:
        body["preview"] = preview
        if job == "schedule":
            body["diagnostics"] = [_upcoming_diagnostic(match) for match in selected]
        elif job == "digest":
            body["diagnostics"] = [_match_diagnostic(match) for match in selected]
        body["media_card_enabled"] = TELEGRAM_MEDIA_CARDS
        body["media_card_ready"] = media_card is not None
        if media_card_error:
            body["media_card_error"] = media_card_error
    return {"statusCode": 502 if failures else 200, "body": json.dumps(body, ensure_ascii=False)}


def handler(event: Dict[str, Any] | None, context: Any) -> Dict[str, Any]:
    """Yandex Cloud Functions entry point."""

    # по умолчанию берём максимум
    limit = MAX_MATCHES
    source: SourceName = _parse_source(None)
    mode = _parse_mode(None)
    dry_run = False
    include_filtered = False
    job = "results"
    test_run_id: str | None = None
    try:
        event = _unwrap_timer_event(event)
        if isinstance(event, dict):
            requested_job = event.get("job", "results")
            if requested_job not in CONTENT_JOBS:
                raise ValueError("job must be results, schedule, or digest")
            job = requested_job
            requested_test_run_id = event.get("test_run_id")
            if requested_test_run_id is not None:
                if job != "schedule":
                    raise ValueError("test_run_id is supported only for schedule")
                if not isinstance(requested_test_run_id, str) or not TEST_RUN_ID_PATTERN.fullmatch(
                    requested_test_run_id
                ):
                    raise ValueError("test_run_id is invalid")
                test_run_id = requested_test_run_id
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
        if job in {"schedule", "digest"} and not PANDASCORE_API_TOKEN:
            missing_config.append("match_source_credentials")
        elif source == "pandascore" and not PANDASCORE_API_TOKEN:
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

    if job in {"schedule", "digest"}:
        return _handle_content_job(job, dry_run, test_run_id)

    try:
        log_event(logger, logging.INFO, "handler_start", limit=limit, source=source, mode=mode, dry_run=dry_run)
        rejected_matches: List[MatchNormalized] = []
        matches = asyncio.run(
            get_new_finished_matches(
                limit=limit,
                source=source,
                dry_run=dry_run,
                include_filtered=include_filtered,
                check_processed=False,
                rejected_matches=rejected_matches,
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

    unconfirmed_tier1 = [
        match
        for match in rejected_matches
        if match.filter_reason == "lan_unconfirmed" and is_tier1_candidate(match)
    ]
    if unconfirmed_tier1 and not dry_run:
        examples = "; ".join(
            f"{match.team1_name} — {match.team2_name} ({match.tournament_name})"
            for match in unconfirmed_tier1[:3]
        )
        log_event(
            logger,
            logging.WARNING,
            "tier1_lan_unconfirmed",
            matches=len(unconfirmed_tier1),
            examples=examples,
        )
        _notify_admin(
            "tier1_lan_unconfirmed",
            (
                f"Найдены Tier-1 матчи без подтверждения LAN: {len(unconfirmed_tier1)}. "
                f"Публикация остановлена. Примеры: {examples}"
            ),
        )

    channel_stats: Dict[str, int] = {}
    skipped_duplicates = 0
    skipped_filtered = 0
    sent_messages = 0
    failed_messages = 0

    for match in matches:
        if isinstance(match, MatchNormalized) and not match.is_tier1_lan and not include_filtered:
            skipped_filtered += 1
            continue

        result_card: bytes | None = None
        result_card_attempted = False

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

            if TELEGRAM_MEDIA_CARDS and not result_card_attempted:
                result_card_attempted = True
                try:
                    result_card = render_result_card(match)
                except Exception as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "media_card_fallback",
                        job="results",
                        match_uid=match.match_uid,
                        error_type=type(exc).__name__,
                        error=_safe_error_message(exc),
                    )

            try:
                text = format_match(match)
                if result_card is not None:
                    try:
                        send_photo_to_telegram(
                            channel["chat_id"],
                            result_card,
                            text,
                            has_spoiler=TELEGRAM_SPOILERS,
                            filename=f"cs2-result-{match.match_id or 'match'}.png",
                        )
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "media_card_delivery_fallback",
                            job="results",
                            channel=name,
                            match_uid=match.match_uid,
                            error_type=type(exc).__name__,
                            error=_safe_error_message(exc),
                        )
                        send_to_telegram(channel["chat_id"], text)
                else:
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
        "tier1_lan_unconfirmed": len(unconfirmed_tier1),
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
        "tier1_lan_unconfirmed": len(unconfirmed_tier1),
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
    if dry_run:
        diagnostic_matches: List[MatchNormalized] = []
        seen_match_uids: set[str] = set()
        for match in [*unconfirmed_tier1, *matches, *rejected_matches]:
            if not isinstance(match, MatchNormalized) or match.match_uid in seen_match_uids:
                continue
            diagnostic_matches.append(match)
            seen_match_uids.add(match.match_uid)
        body["diagnostics"] = [
            _match_diagnostic(match) for match in diagnostic_matches[:MAX_MATCHES]
        ]
    return {
        "statusCode": 502 if failed_messages else 200,
        "body": json.dumps(body),
    }
