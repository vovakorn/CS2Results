"""Entry point for Yandex Cloud Functions."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sys
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .config import (
    BOT_MODE,
    CHANNELS,
    TELEGRAM_ADMIN_CHAT_ID,
    TELEGRAM_MEDIA_CARDS,
    TELEGRAM_PROXY_URL,
    TELEGRAM_SPOILERS,
    TELEGRAM_TOKEN,
)
from .analytics import record_manual_post_metrics, record_post, record_subscriber_snapshot
from .logging_utils import log_event
from .media_cards import (
    MAX_RESULT_MATCHES,
    MAX_SCHEDULE_TOTAL_MATCHES,
    can_render_final_card,
    render_final_card,
    render_result_card,
    render_results_card,
    render_schedule_cards,
    render_tournament_radar_card,
)
from .match_sources.config import (
    DISPLAY_TIMEZONE,
    ENABLE_LIQUIPEDIA_FALLBACK,
    LIQUIPEDIA_API_KEY,
    MATCH_SOURCE,
    OBJECT_STORAGE_BUCKET,
    PANDASCORE_API_TOKEN,
    POPULAR_TEAMS,
)
from .match_sources.filters import is_tier1_candidate, tier1_autopilot_decision
from .match_sources.match_fetcher import SourceName, apply_quality_filters, get_new_finished_matches
from .match_sources.models import (
    MatchNormalized,
    ScheduleMatchContext,
    TournamentRadar,
    UpcomingMatchNormalized,
)
from .match_sources.sources.pandascore_context import (
    fetch_schedule_match_context,
    fetch_tournament_radar,
)
from .match_sources.sources.pandascore_source import (
    fetch_finished_matches as fetch_pandascore_finished_matches,
    fetch_upcoming_matches,
)
from .match_sources.storage import (
    PendingDelivery,
    claim_admin_alert,
    claim_channel_delivery,
    claim_content_delivery,
    clear_telegram_media_degraded,
    delete_result_delivery,
    enqueue_result_delivery,
    is_channel_processed,
    is_telegram_media_degraded,
    list_pending_result_deliveries,
    mark_channel_processed,
    mark_content_processed,
    mark_delivery_claim_sent,
    mark_telegram_media_degraded,
    record_result_delivery_attempt,
    reconcile_channel_delivery,
    reconcile_content_delivery,
    release_delivery_claim,
    result_outbox_key,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Cloud Functions may install a handler on stderr that is not forwarded to
# Cloud Logging for application output. Ensure the root logger also emits the
# structured events to stdout, while avoiding duplicate handlers on warm starts.
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not any(
    getattr(handler, "_cs2results_stdout", False)
    for handler in root_logger.handlers
):
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    stdout_handler._cs2results_stdout = True
    root_logger.addHandler(stdout_handler)

TELEGRAM_API_URL = "https://api.telegram.org"
TELEGRAM_METHOD = "sendMessage"

MIN_MATCHES = 1
MAX_MATCHES = 30
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
MAX_TELEGRAM_CAPTION_LENGTH = 1024
RESULT_MEDIA_BUDGET_SECONDS = 35.0
RESULT_TELEGRAM_TIMEOUT_SECONDS = 10
RESULT_TELEGRAM_MAX_ATTEMPTS = 2
# Results make up to two bounded photo attempts, but retry only before a TCP
# connection exists. A confirmed or uncertain Telegram request is never retried.
# The durable outbox and five-minute trigger provide further spaced retries.
RESULT_TEXT_TELEGRAM_MAX_ATTEMPTS = 1
RESULT_DELIVERY_START_BUDGET_SECONDS = 90.0
RESULT_MEDIA_DEGRADED_SECONDS = 60 * 60
RESULT_OUTBOX_LIMIT = 200
_monotonic = time.monotonic
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
CONTENT_JOBS = {"results", "schedule", "digest", "radar", "analytics"}
TEST_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_SCHEDULE_DAYS_AHEAD = 7


class TelegramDeliveryError(RuntimeError):
    """A safe Telegram failure that never contains the bot token or request URL."""


class TelegramConnectTimeoutError(TelegramDeliveryError):
    """The Telegram TCP connection was not established, so retrying is safe."""


class TelegramDeliveryUncertainError(TelegramDeliveryError):
    """Telegram may have accepted the request, so retrying could duplicate a post."""


def _record_post_analytics(channel_id: str, content_uid: str, job: str, **metadata: Any) -> None:
    """Analytics must never turn a delivered Telegram post into a failed job."""
    try:
        asyncio.run(record_post(channel_id, content_uid, job, metadata=metadata))
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "analytics_post_record_failed",
            channel=channel_id,
            job=job,
            error_type=type(exc).__name__,
        )


def _match_diagnostic(match: MatchNormalized) -> Dict[str, Any]:
    """Return public source fields that are safe to expose in dry-run output."""
    autopilot_selected, autopilot_reason = tier1_autopilot_decision(match)
    return {
        "source": match.source,
        "match_id": match.match_id,
        "tournament": match.tournament_name,
        "competition_key": match.competition_key,
        "source_refs": match.source_refs.model_dump() if match.source_refs else None,
        "tournament_tier": match.tournament_tier,
        "tournament_tier_type": match.tournament_tier_type,
        "publisher_tier": match.publisher_tier,
        "tournament_section": match.tournament_section,
        "teams": [match.team1_name, match.team2_name],
        "score": [match.score1, match.score2],
        "date": match.date,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "original_scheduled_at": match.original_scheduled_at,
        "rescheduled": match.rescheduled,
        "forfeit": match.forfeit,
        "result_type": match.result_type,
        "team_result_statuses": [match.team1_result_status, match.team2_result_status],
        "date_exact": match.date_exact,
        "vod_url": match.vod_url,
        "is_lan": match.is_lan,
        "location": match.location,
        "is_tier1_lan": match.is_tier1_lan,
        "filter_reason": match.filter_reason,
        "tier1_autopilot_selected": autopilot_selected,
        "tier1_autopilot_reason": autopilot_reason,
    }


def _upcoming_diagnostic(match: UpcomingMatchNormalized) -> Dict[str, Any]:
    """Return public schedule fields without exposing full remote image URLs."""
    autopilot_selected, autopilot_reason = tier1_autopilot_decision(match)
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
        "selected": match.is_featured,
        "filter_reason": None if match.is_featured else match.feature_reason,
        "is_featured": match.is_featured,
        "feature_reason": match.feature_reason,
        "tier1_autopilot_selected": autopilot_selected,
        "tier1_autopilot_reason": autopilot_reason,
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
    for secret in (TELEGRAM_TOKEN, TELEGRAM_PROXY_URL, PANDASCORE_API_TOKEN, LIQUIPEDIA_API_KEY):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"/bot[^/\s]+/", "/bot[REDACTED]/", message, flags=re.IGNORECASE)
    return message[:500] or type(exc).__name__


def _telegram_request_options() -> Dict[str, Any]:
    """Return shared HTTP options without relying on ambient proxy variables."""
    if not TELEGRAM_PROXY_URL:
        return {}
    return {"proxies": {"http": TELEGRAM_PROXY_URL, "https": TELEGRAM_PROXY_URL}}


def _retry_connect_timeout(attempt: int, attempts: int) -> bool:
    """Retry only before a TCP connection exists; no Telegram request was sent."""
    if attempt >= attempts:
        return False
    time.sleep(min(2 ** (attempt - 1), 5))
    return True


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
                **_telegram_request_options(),
            )
        except requests.ConnectTimeout:
            log_event(
                logger,
                logging.WARNING,
                "telegram_request_connect_timeout",
                delivery_format="text",
                attempt=attempt,
                max_attempts=attempts,
            )
            if _retry_connect_timeout(attempt, attempts):
                continue
            raise TelegramConnectTimeoutError("Telegram connection could not be established") from None
        except requests.RequestException:
            raise TelegramDeliveryUncertainError("Telegram request outcome is unknown") from None

        if response.status_code == 429:
            if attempt < attempts:
                retry_after = 0
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 0))
                except (TypeError, ValueError, AttributeError, requests.JSONDecodeError):
                    retry_after = 0
                time.sleep(min(max(retry_after, 2 ** (attempt - 1)), 5))
                continue
        if response.status_code >= 500:
            raise TelegramDeliveryUncertainError(
                f"Telegram API returned HTTP {response.status_code}"
            )
        if response.status_code >= 300:
            raise TelegramDeliveryError(f"Telegram API returned HTTP {response.status_code}")

        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise TelegramDeliveryUncertainError("Telegram API returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise TelegramDeliveryError("Telegram API rejected the message")
        data["_cs2results_attempt"] = attempt
        return data

    raise TelegramDeliveryError("Telegram request failed")


def get_telegram_member_count(chat_id: str | int) -> int:
    """Read a channel subscriber snapshot; bot must be an administrator."""
    if not TELEGRAM_TOKEN:
        raise TelegramDeliveryError("Telegram credentials are not configured")
    try:
        response = requests.post(
            f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/getChatMemberCount",
            json={"chat_id": str(chat_id)},
            timeout=7,
            allow_redirects=False,
            **_telegram_request_options(),
        )
    except requests.RequestException:
        raise TelegramDeliveryUncertainError("Telegram member-count request outcome is unknown") from None
    if response.status_code >= 500:
        raise TelegramDeliveryUncertainError(f"Telegram API returned HTTP {response.status_code}")
    if response.status_code >= 300:
        raise TelegramDeliveryError(f"Telegram API returned HTTP {response.status_code}")
    try:
        data = response.json()
    except requests.JSONDecodeError:
        raise TelegramDeliveryUncertainError("Telegram API returned invalid member count") from None
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), int):
        raise TelegramDeliveryError("Telegram API returned invalid member count")
    return data["result"]


def _handle_analytics_job(event: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    operation = event.get("analytics_operation", "snapshot")
    if operation not in {"snapshot", "import_metrics"}:
        return _error_response(400, "invalid_request")
    if operation == "import_metrics":
        channel_id = event.get("channel_id")
        message_id = event.get("message_id")
        if not isinstance(channel_id, str) or not channel_id or not isinstance(message_id, int):
            return _error_response(400, "invalid_request")
        views = event.get("views_24h")
        reactions = event.get("reactions")
        if any(value is not None and (not isinstance(value, int) or value < 0) for value in (views, reactions)):
            return _error_response(400, "invalid_request")
        if not dry_run:
            asyncio.run(record_manual_post_metrics(channel_id, message_id, views, reactions))
        return {"statusCode": 200, "body": json.dumps({"job": "analytics", "operation": operation, "dry_run": dry_run})}
    snapshots = []
    for channel in _iter_channels():
        channel_id = str(channel.get("id") or channel.get("name", "unknown"))
        try:
            count = get_telegram_member_count(channel["chat_id"])
        except TelegramDeliveryError as exc:
            log_event(
                logger,
                logging.ERROR,
                "analytics_telegram_delivery_failed",
                channel=channel_id,
                error=_safe_error_message(exc),
            )
            return _error_response(502, "telegram_delivery_unavailable")
        snapshots.append({"channel_id": channel_id, "member_count": count})
        if not dry_run:
            asyncio.run(record_subscriber_snapshot(channel_id, count))
    return {"statusCode": 200, "body": json.dumps({"job": "analytics", "operation": operation, "snapshots": snapshots, "dry_run": dry_run})}


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
                **_telegram_request_options(),
            )
        except requests.ConnectTimeout:
            log_event(
                logger,
                logging.WARNING,
                "telegram_request_connect_timeout",
                delivery_format="photo",
                attempt=attempt,
                max_attempts=attempts,
            )
            if _retry_connect_timeout(attempt, attempts):
                continue
            raise TelegramConnectTimeoutError("Telegram photo connection could not be established") from None
        except requests.RequestException:
            raise TelegramDeliveryUncertainError("Telegram photo request outcome is unknown") from None

        if response.status_code == 429:
            if attempt < attempts:
                retry_after = 0
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 0))
                except (TypeError, ValueError, AttributeError, requests.JSONDecodeError):
                    retry_after = 0
                time.sleep(min(max(retry_after, 2 ** (attempt - 1)), 5))
                continue
        if response.status_code >= 500:
            raise TelegramDeliveryUncertainError(
                f"Telegram API returned HTTP {response.status_code}"
            )
        if response.status_code >= 300:
            raise TelegramDeliveryError(f"Telegram API returned HTTP {response.status_code}")
        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise TelegramDeliveryUncertainError("Telegram API returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise TelegramDeliveryError("Telegram API rejected the photo")
        data["_cs2results_attempt"] = attempt
        return data

    raise TelegramDeliveryError("Telegram photo request failed")


def send_media_group_to_telegram(
    chat_id: str | int,
    photos: Sequence[bytes],
    caption: str,
    *,
    filenames: Sequence[str] | None = None,
    has_spoiler: bool = False,
    timeout: int = 15,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Send two or more PNGs as one Telegram album."""
    if not TELEGRAM_TOKEN:
        raise TelegramDeliveryError("Telegram credentials are not configured")
    if not 2 <= len(photos) <= 10:
        raise TelegramDeliveryError("Telegram media group requires two to ten photos")
    if any(not photo for photo in photos):
        raise TelegramDeliveryError("Telegram media group contains an empty photo")
    if len(caption) > MAX_TELEGRAM_CAPTION_LENGTH:
        raise TelegramDeliveryError("Telegram media group caption is too long")
    if filenames is None:
        filenames = [f"cs2-schedule-{index}.png" for index in range(1, len(photos) + 1)]
    if len(filenames) != len(photos):
        raise TelegramDeliveryError("Telegram media group filenames do not match photos")

    media = []
    files = {}
    for index, (photo, filename) in enumerate(zip(photos, filenames, strict=True)):
        attachment_name = f"photo{index}"
        item: Dict[str, Any] = {
            "type": "photo",
            "media": f"attach://{attachment_name}",
            "has_spoiler": has_spoiler,
        }
        if index == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        files[attachment_name] = (filename, photo, "image/png")

    url = f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/sendMediaGroup"
    payload = {"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)}
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                data=payload,
                files=files,
                timeout=timeout,
                allow_redirects=False,
                **_telegram_request_options(),
            )
        except requests.ConnectTimeout:
            log_event(
                logger,
                logging.WARNING,
                "telegram_request_connect_timeout",
                delivery_format="media_group",
                attempt=attempt,
                max_attempts=attempts,
            )
            if _retry_connect_timeout(attempt, attempts):
                continue
            raise TelegramConnectTimeoutError("Telegram media group connection could not be established") from None
        except requests.RequestException:
            raise TelegramDeliveryUncertainError("Telegram media group request outcome is unknown") from None

        if response.status_code == 429:
            if attempt < attempts:
                retry_after = 0
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 0))
                except (TypeError, ValueError, AttributeError, requests.JSONDecodeError):
                    retry_after = 0
                time.sleep(min(max(retry_after, 2 ** (attempt - 1)), 5))
                continue
        if response.status_code >= 500:
            raise TelegramDeliveryUncertainError(
                f"Telegram API returned HTTP {response.status_code}"
            )
        if response.status_code >= 300:
            raise TelegramDeliveryError(
                f"Telegram API returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise TelegramDeliveryUncertainError("Telegram API returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise TelegramDeliveryError("Telegram API rejected the media group")
        data["_cs2results_attempt"] = attempt
        return data

    raise TelegramDeliveryError("Telegram media group request failed")


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


def _truncate_telegram_html(message: str, limit: int) -> str:
    """Truncate HTML without leaving Telegram tags or entities incomplete."""
    if len(message) <= limit:
        return message
    if limit <= 1:
        return "…"[:limit]

    tokens = re.findall(r"<[^>]*>|[^<]+", message)
    output: List[str] = []
    output_length = 0
    open_tags: List[str] = []

    for token in tokens:
        if token.startswith("<"):
            tag_match = re.match(r"</?([A-Za-z][\w-]*)", token)
            tag_name = tag_match.group(1).casefold() if tag_match else ""
            is_closing = token.startswith("</")
            is_self_closing = token.rstrip().endswith("/>")
            if is_closing:
                if output_length + len(token) <= limit:
                    if open_tags and open_tags[-1] == tag_name:
                        open_tags.pop()
                    output.append(token)
                    output_length += len(token)
                else:
                    break
                continue
            if output_length + len(token) > limit:
                break
            output.append(token)
            output_length += len(token)
            if tag_name and not is_self_closing:
                open_tags.append(tag_name)
            continue

        closing_length = sum(len(f"</{tag_name}>") for tag_name in open_tags)
        if output_length + len(token) + closing_length <= limit:
            output.append(token)
            output_length += len(token)
            continue

        available = limit - output_length - closing_length - 1
        if available > 0:
            visible_text = html.unescape(token)
            prefix = ""
            for character in visible_text:
                candidate = html.escape(prefix + character)
                if len(candidate) > available:
                    break
                prefix += character
            escaped_prefix = html.escape(prefix)
            output.append(escaped_prefix + "…")
            output_length += len(escaped_prefix) + 1
        else:
            if output_length + closing_length < limit:
                output.append("…")
                output_length += 1
        break

    # A truncation can happen inside an open tag. Close only tags that were
    # emitted; all omitted content is intentionally discarded.
    for tag_name in reversed(open_tags):
        closing_tag = f"</{tag_name}>"
        if output_length + len(closing_tag) > limit:
            break
        output.append(closing_tag)
        output_length += len(closing_tag)
    return "".join(output)


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
        if TELEGRAM_SPOILERS:
            safe_score = f"<tg-spoiler>{safe_score}</tg-spoiler>"
        pieces.append(f"<b>{safe_team1}</b>  {safe_score}  <b>{safe_team2}</b>")
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
    return _truncate_telegram_html(message, MAX_TELEGRAM_MESSAGE_LENGTH)


def _local_day_window(
    now: datetime | None = None,
    days_ahead: int = 1,
) -> tuple[datetime, datetime, datetime]:
    try:
        local_timezone = ZoneInfo(DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("DISPLAY_TIMEZONE is invalid") from exc
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    local_now = reference.astimezone(local_timezone)
    start = datetime.combine(local_now.date(), datetime_time.min, tzinfo=local_timezone)
    end_date = local_now.date() + timedelta(days=days_ahead)
    end = datetime.combine(end_date, datetime_time.min, tzinfo=local_timezone)
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
    sorted_matches = sorted(
        matches,
        key=lambda item: _parse_datetime(item.scheduled_at) or datetime.max.replace(tzinfo=timezone.utc),
    )
    groups: dict[str, list[UpcomingMatchNormalized]] = {}
    for match in sorted_matches:
        tournament_name = getattr(match, "tournament_name", None)
        group_name = tournament_name.strip() if isinstance(tournament_name, str) and tournament_name.strip() else "Другие матчи"
        groups.setdefault(group_name, []).append(match)

    entries: list[tuple[list[str], int]] = []
    for tournament_name, tournament_matches in groups.items():
        for match_index, match in enumerate(tournament_matches):
            parsed = _parse_datetime(match.scheduled_at)
            local_time = parsed.astimezone(timezone_info).strftime("%H:%M") if parsed else "—"
            match_line = f"{local_time} — <b>{html.escape(match.team1_name)} vs {html.escape(match.team2_name)}</b>"
            if match_index == 0:
                entries.append(
                    ([f"🏆 <b>{html.escape(_display_tournament_name(tournament_name))}</b>", match_line], 1)
                )
            else:
                entries.append(([match_line], 1))
        entries.append(([""], 0))

    footer = ["Источник: PandaScore", "", "#CS2 #РасписаниеМатчей"]
    lines = list(header)
    omitted = 0
    for index, (entry, match_count) in enumerate(entries):
        omitted_if_stopped = match_count + sum(next_match_count for _, next_match_count in entries[index + 1 :])
        suffix = ([f"… и ещё {omitted_if_stopped} матчей", ""] if omitted_if_stopped else []) + footer
        if len("\n".join(lines + entry + suffix).strip()) > MAX_TELEGRAM_MESSAGE_LENGTH:
            omitted = omitted_if_stopped
            break
        lines.extend(entry)
    if omitted:
        lines.extend([f"… и ещё {omitted} матчей", ""])
    lines.extend(footer)
    return "\n".join(lines).strip()


def _display_tournament_name(tournament_name: str) -> str:
    """Make PandaScore's league/serie/stage label easier to scan in a post."""
    parts = [part.strip() for part in tournament_name.split(" — ") if part.strip()]
    if len(parts) >= 3 and parts[1].startswith("20"):
        return f"{parts[0]} {parts[1]} · {' · '.join(parts[2:])}"
    return " · ".join(parts) if parts else "Другие матчи"


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


def _context_priority(match: UpcomingMatchNormalized) -> tuple[int, int, str]:
    tier_rank = {"s": 0, "a": 1}.get(match.tournament_tier or "", 2)
    popular = {
        MatchNormalized._identity_part(team)
        for team in POPULAR_TEAMS
    }
    match_teams = {
        MatchNormalized._identity_part(match.team1_name),
        MatchNormalized._identity_part(match.team2_name),
    }
    popular_rank = 0 if popular & match_teams else 1
    return tier_rank, popular_rank, match.scheduled_at


def _russian_count(count: int, forms: tuple[str, str, str]) -> str:
    """Return a small Russian count phrase, e.g. ``4 победы``."""
    if 11 <= count % 100 <= 14:
        form = forms[2]
    elif count % 10 == 1:
        form = forms[0]
    elif 2 <= count % 10 <= 4:
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


def _head_to_head_takeaway(context: ScheduleMatchContext) -> str | None:
    record = context.head_to_head
    if record is None:
        return None
    if record.match_count == 0:
        return "<b>Очные встречи за 3 месяца:</b> команды не встречались."
    return (
        f"<b>Очные встречи за 3 месяца:</b> "
        f"{_russian_count(record.match_count, ('матч', 'матча', 'матчей'))}. "
        f"<b>{html.escape(context.team1_form.team_name.upper())}</b>  "
        f"{record.team1_wins} : {record.team2_wins}  "
        f"<b>{html.escape(context.team2_form.team_name.upper())}</b>."
    )


def _context_tournament_name(match: UpcomingMatchNormalized) -> str:
    """Use the shared competition label instead of its individual group/stage."""
    if match.competition_key:
        return match.competition_key
    parts = [part.strip() for part in match.tournament_name.split(" — ") if part.strip()]
    if len(parts) > 1 and re.fullmatch(r"(?:group|группа)\s+[\w\d]+", parts[-1], re.IGNORECASE):
        return " — ".join(parts[:-1])
    return match.tournament_name


def format_schedule_context(
    matches: Sequence[UpcomingMatchNormalized],
    contexts: dict[str, ScheduleMatchContext],
) -> str:
    """Format optional, compact pre-match context for every scheduled fixture."""
    context_matches = [
        (match, contexts.get(match.match_id))
        for match in sorted(matches, key=_context_priority)
    ]
    if not context_matches:
        return ""

    grouped_matches: dict[tuple[str, int | None], list[tuple[UpcomingMatchNormalized, ScheduleMatchContext | None]]] = {}
    for match, context in context_matches:
        group_key = (_context_tournament_name(match), match.best_of)
        grouped_matches.setdefault(group_key, []).append((match, context))

    lines = ["🔎 <b>Контекст к матчам дня</b>", ""]
    for (tournament_name, best_of), tournament_matches in grouped_matches.items():
        format_note = f"Bo{best_of}" if best_of else "формат уточняется"
        lines.append(f"🏆 Турнир {html.escape(tournament_name)} · {format_note}")
        lines.append("")
        for match, context in tournament_matches:
            lines.append(f"<b>{html.escape(match.team1_name)} — {html.escape(match.team2_name)}</b>")
            if context is None:
                lines.append("Контекст по командам пока недоступен.")
            else:
                head_to_head = _head_to_head_takeaway(context)
                if head_to_head:
                    lines.append(head_to_head)
                lines.append(
                    f"<b>Последние 5 матчей каждой команды:</b> {html.escape(context.team1_form.team_name)} — "
                    f"{_russian_count(context.team1_form.wins, ('победа', 'победы', 'побед'))} и "
                    f"{_russian_count(context.team1_form.losses, ('поражение', 'поражения', 'поражений'))}; "
                    f"{html.escape(context.team2_form.team_name)} — "
                    f"{_russian_count(context.team2_form.wins, ('победа', 'победы', 'побед'))} и "
                    f"{_russian_count(context.team2_form.losses, ('поражение', 'поражения', 'поражений'))}."
                )
            lines.append("")
    lines.extend(
        [
            "ℹ️ Это последние результаты, а не рейтинг команд и не прогноз на матч.",
            "",
            "Источник: PandaScore",
            "",
            "#CS2 #КонтекстМатча",
        ]
    )
    return "\n".join(lines).strip()[:MAX_TELEGRAM_MESSAGE_LENGTH]


def format_tournament_radar(radar: TournamentRadar, tournament_name: str) -> str:
    lines = [f"🏆 <b>Турнирный радар — {html.escape(tournament_name)}</b>", ""]
    if radar.standings:
        lines.extend(["<b>Положение:</b>", *[html.escape(line) for line in radar.standings], ""])
    if radar.roster_team_count:
        lines.append(f"Участников: {radar.roster_team_count}")
    if radar.bracket_match_count:
        lines.append(f"Матчей в сетке: {radar.bracket_match_count}")
    if len(lines) == 2:
        lines.append("Данные сетки и положения пока не опубликованы организатором.")
    lines.extend(["", "Источник: PandaScore", "", "#CS2 #ТурнирныйРадар"])
    return "\n".join(lines)[:MAX_TELEGRAM_MESSAGE_LENGTH]


async def _fetch_schedule_contexts(
    matches: Sequence[UpcomingMatchNormalized],
) -> list[ScheduleMatchContext | Exception]:
    return list(
        await asyncio.gather(
            *(fetch_schedule_match_context(match) for match in matches),
            return_exceptions=True,
        )
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
    include_filtered: bool = False,
    days_ahead: int = 1,
) -> Dict[str, Any]:
    start, end, local_now = _local_day_window(days_ahead=days_ahead)
    schedule_context_text = ""
    schedule_contexts: dict[str, ScheduleMatchContext] = {}
    try:
        if job == "schedule":
            fetched = asyncio.run(fetch_upcoming_matches(start, end))
            selected = fetched
            text = format_daily_schedule(selected, local_now) if selected else ""
            context_matches = sorted(selected, key=_context_priority)
            if context_matches:
                context_results = asyncio.run(_fetch_schedule_contexts(context_matches))
                for match, result in zip(context_matches, context_results):
                    if isinstance(result, ScheduleMatchContext):
                        schedule_contexts[match.match_id] = result
                    elif isinstance(result, Exception):
                        log_event(
                            logger,
                            logging.WARNING,
                            "schedule_context_unavailable",
                            match_id=match.match_id,
                            error_type=type(result).__name__,
                        )
                schedule_context_text = format_schedule_context(selected, schedule_contexts)
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
        if dry_run and job == "schedule":
            diagnostic_matches = fetched if include_filtered else selected
            body.update(
                {
                    "days_ahead": days_ahead,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "preview": None,
                    "diagnostics": [
                        _upcoming_diagnostic(match) for match in diagnostic_matches
                    ],
                }
            )
        return {"statusCode": 200, "body": json.dumps(body, ensure_ascii=False)}

    media_cards: list[bytes] = []
    media_card_error: str | None = None
    card_supported = (
        job == "schedule" and 1 <= len(selected) <= MAX_SCHEDULE_TOTAL_MATCHES
    ) or (
        job == "digest" and 1 <= len(selected) <= MAX_RESULT_MATCHES
    )
    if TELEGRAM_MEDIA_CARDS and card_supported:
        try:
            if job == "schedule":
                media_cards = render_schedule_cards(selected, local_now, DISPLAY_TIMEZONE)
                if len(media_cards) > 10:
                    media_card_error = "too_many_tournament_cards"
                    media_cards = []
                    log_event(
                        logger,
                        logging.WARNING,
                        "media_card_fallback",
                        job=job,
                        reason="too_many_tournament_cards",
                    )
            else:
                media_cards = [render_results_card(selected, local_now)]
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
        telegram_confirmed = False
        try:
            if asyncio.run(reconcile_content_delivery(content_uid, job)):
                log_event(logger, logging.INFO, "delivery_state_reconciled", channel=channel_id, job=job)
                continue
            claim = asyncio.run(claim_content_delivery(content_uid))
            if claim is None:
                duplicates += 1
                continue
            if media_cards:
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
                    if job == "schedule" and len(media_cards) > 1:
                        filenames = [
                            f"cs2-schedule-{day_key}-{index}-of-{len(media_cards)}.png"
                            for index in range(1, len(media_cards) + 1)
                        ]
                        send_media_group_to_telegram(
                            channel["chat_id"],
                            media_cards,
                            caption,
                            filenames=filenames,
                        )
                    else:
                        send_photo_to_telegram(
                            channel["chat_id"],
                            media_cards[0],
                            caption,
                            has_spoiler=has_spoiler,
                            filename=filename,
                        )
                except TelegramDeliveryUncertainError:
                    raise
                except TelegramDeliveryError as exc:
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
            telegram_confirmed = True
            claim = asyncio.run(mark_delivery_claim_sent(claim))
            if job == "schedule" and schedule_context_text:
                try:
                    send_to_telegram(channel["chat_id"], schedule_context_text)
                except Exception as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "schedule_context_delivery_failed",
                        channel=channel_id,
                        error_type=type(exc).__name__,
                        error=_safe_error_message(exc),
                    )
            asyncio.run(mark_content_processed(content_uid, job))
            _record_post_analytics(
                channel_id,
                content_uid,
                job,
                matches_selected=len(selected),
                media_card=bool(media_cards),
            )
            sent += 1
        except Exception as exc:
            failures += 1
            delivery_uncertain = isinstance(exc, TelegramDeliveryUncertainError)
            if delivery_uncertain:
                _notify_admin(
                    "telegram_delivery_uncertain",
                    f"Telegram не подтвердил исход доставки выпуска «{job}» в канал {channel_id}; повтор отключён.",
                )
            if claim is not None and not telegram_confirmed and not delivery_uncertain:
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
            diagnostic_matches = fetched if include_filtered else selected
            body["days_ahead"] = days_ahead
            body["window_start"] = start.isoformat()
            body["window_end"] = end.isoformat()
            body["diagnostics"] = [
                _upcoming_diagnostic(match) for match in diagnostic_matches
            ]
            body["context_preview"] = schedule_context_text or None
            body["context_matches_ready"] = len(schedule_contexts)
        elif job == "digest":
            body["diagnostics"] = [_match_diagnostic(match) for match in selected]
        body["media_card_enabled"] = TELEGRAM_MEDIA_CARDS
        body["media_card_ready"] = bool(media_cards)
        body["media_card_count"] = len(media_cards)
        if media_card_error:
            body["media_card_error"] = media_card_error
    return {"statusCode": 502 if failures else 200, "body": json.dumps(body, ensure_ascii=False)}


def _handle_radar_job(
    tournament_id: str,
    tournament_name: str,
    dry_run: bool,
    test_run_id: str | None = None,
    card_variant: str = "auto",
) -> Dict[str, Any]:
    try:
        radar = asyncio.run(fetch_tournament_radar(tournament_id))
        text = format_tournament_radar(radar, tournament_name)
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "tournament_radar_fetch_failed",
            tournament_id=tournament_id,
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )
        return _error_response(502, "match_source_unavailable")

    media_card: bytes | None = None
    media_card_error: str | None = None
    if TELEGRAM_MEDIA_CARDS:
        try:
            media_card = render_tournament_radar_card(
                radar, tournament_name, DISPLAY_TIMEZONE, card_variant
            )
        except Exception as exc:
            media_card_error = type(exc).__name__
            log_event(
                logger,
                logging.WARNING,
                "tournament_radar_card_unavailable",
                tournament_id=tournament_id,
                error_type=type(exc).__name__,
            )

    sent = 0
    duplicates = 0
    failures = 0
    # Keep radar deduplication aligned with the channel's displayed calendar day.
    day_key = _local_day_window()[2].date().isoformat()
    for channel in _iter_channels():
        if dry_run:
            sent += 1
            continue
        channel_id = str(channel.get("id") or channel.get("name", "unknown"))
        content_uid = f"radar_{tournament_id}_{day_key}_{channel_id}"
        if test_run_id:
            content_uid = f"{content_uid}_test_{test_run_id}"
        claim = None
        telegram_confirmed = False
        try:
            if asyncio.run(reconcile_content_delivery(content_uid, "radar")):
                log_event(logger, logging.INFO, "delivery_state_reconciled", channel=channel_id, job="radar")
                continue
            claim = asyncio.run(claim_content_delivery(content_uid))
            if claim is None:
                duplicates += 1
                continue
            if media_card:
                caption = f"🏆 <b>Турнирный радар</b>\n{html.escape(tournament_name)}\n\nИсточник: PandaScore"
                if test_run_id:
                    caption = f"🧪 <b>Тестовая карточка</b>\n\n{caption}"
                try:
                    send_photo_to_telegram(
                        channel["chat_id"],
                        media_card,
                        caption,
                        filename=f"cs2-radar-{tournament_id}-{day_key}.png",
                    )
                except TelegramDeliveryUncertainError:
                    raise
                except TelegramDeliveryError as exc:
                    media_card_error = type(exc).__name__
                    log_event(
                        logger,
                        logging.WARNING,
                        "tournament_radar_card_delivery_fallback",
                        tournament_id=tournament_id,
                        channel=channel_id,
                        error_type=type(exc).__name__,
                    )
                    send_to_telegram(channel["chat_id"], text)
            else:
                send_to_telegram(channel["chat_id"], text)
            telegram_confirmed = True
            claim = asyncio.run(mark_delivery_claim_sent(claim))
            asyncio.run(mark_content_processed(content_uid, "radar"))
            _record_post_analytics(
                channel_id,
                content_uid,
                "radar",
                tournament_id=tournament_id,
                radar_card_variant=card_variant,
                media_card=bool(media_card),
            )
            sent += 1
        except Exception as exc:
            failures += 1
            if claim is not None and not telegram_confirmed:
                try:
                    asyncio.run(release_delivery_claim(claim))
                except Exception:
                    pass
            log_event(
                logger,
                logging.ERROR,
                "tournament_radar_delivery_failed",
                tournament_id=tournament_id,
                channel=channel_id,
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )

    body = {
        "job": "radar",
        "tournament_id": tournament_id,
        "messages_sent": sent,
        "duplicates_skipped": duplicates,
        "delivery_failures": failures,
        "dry_run": dry_run,
    }
    if dry_run:
        body.update(
            {
                "preview": text,
                "radar": radar.model_dump(),
                "media_card_enabled": TELEGRAM_MEDIA_CARDS,
                "media_card_ready": bool(media_card),
                "radar_card_variant": card_variant,
            }
        )
        if media_card_error:
            body["media_card_error"] = media_card_error
    return {"statusCode": 502 if failures else 200, "body": json.dumps(body, ensure_ascii=False)}


def handler(event: Dict[str, Any] | None, context: Any) -> Dict[str, Any]:
    """Yandex Cloud Functions entry point."""

    handler_started_at = _monotonic()

    # по умолчанию берём максимум
    limit = MAX_MATCHES
    source: SourceName = _parse_source(None)
    mode = _parse_mode(None)
    dry_run = False
    include_filtered = False
    retry_only = False
    days_ahead = 1
    job = "results"
    test_run_id: str | None = None
    tournament_id: str | None = None
    tournament_name = "Турнир"
    radar_card_variant = "auto"
    try:
        event = _unwrap_timer_event(event)
        if isinstance(event, dict):
            requested_job = event.get("job", "results")
            if requested_job not in CONTENT_JOBS:
                raise ValueError("job must be results, schedule, digest, radar, or analytics")
            job = requested_job
            requested_test_run_id = event.get("test_run_id")
            if requested_test_run_id is not None:
                if job not in {"schedule", "radar"}:
                    raise ValueError("test_run_id is supported only for schedule or radar")
                if not isinstance(requested_test_run_id, str) or not TEST_RUN_ID_PATTERN.fullmatch(
                    requested_test_run_id
                ):
                    raise ValueError("test_run_id is invalid")
                test_run_id = requested_test_run_id
            if job == "radar":
                requested_tournament_id = event.get("tournament_id")
                if not isinstance(requested_tournament_id, (str, int)) or not str(
                    requested_tournament_id
                ).strip():
                    raise ValueError("tournament_id is required for radar")
                tournament_id = str(requested_tournament_id).strip()
                requested_tournament_name = event.get("tournament_name", "Турнир")
                if not isinstance(requested_tournament_name, str) or not requested_tournament_name.strip():
                    raise ValueError("tournament_name must be a non-empty string")
                tournament_name = requested_tournament_name.strip()[:300]
                requested_card_variant = event.get("radar_card_variant", "auto")
                if not isinstance(requested_card_variant, str) or requested_card_variant not in {
                    "auto",
                    "standings",
                    "bracket",
                    "next_match",
                }:
                    raise ValueError("radar_card_variant is invalid")
                radar_card_variant = requested_card_variant
            requested = event.get("limit")
            if isinstance(requested, int) and not isinstance(requested, bool):
                limit = max(MIN_MATCHES, min(MAX_MATCHES, requested))
            source = _parse_source(event.get("source"))
            mode = _parse_mode(event.get("mode"))
            dry_run = _parse_bool(event.get("dry_run"), default=False)
            retry_only = _parse_bool(event.get("retry_only"), default=False)
            if retry_only and (job != "results" or dry_run):
                raise ValueError("retry_only is supported only for production results")
            requested_filtered = _parse_bool(event.get("include_filtered"), default=mode == "debug")
            include_filtered = requested_filtered and dry_run
            requested_days_ahead = event.get("days_ahead", 1)
            if not isinstance(requested_days_ahead, int) or isinstance(
                requested_days_ahead, bool
            ):
                raise ValueError("days_ahead must be an integer")
            if not 1 <= requested_days_ahead <= MAX_SCHEDULE_DAYS_AHEAD:
                raise ValueError("days_ahead must be between 1 and 7")
            if requested_days_ahead != 1 and (job != "schedule" or not dry_run):
                raise ValueError("days_ahead above 1 is supported only for schedule dry-run")
            days_ahead = requested_days_ahead
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
        analytics_snapshot = job == "analytics" and (
            not isinstance(event, dict) or event.get("analytics_operation", "snapshot") == "snapshot"
        )
        if job != "analytics" or analytics_snapshot:
            if not TELEGRAM_TOKEN:
                missing_config.append("telegram_credentials")
            if not any(True for _ in _iter_channels()):
                missing_config.append("delivery_channels")
        if not OBJECT_STORAGE_BUCKET:
            missing_config.append("object_storage_bucket")
        if job == "analytics":
            pass
        elif retry_only:
            pass
        elif job in {"schedule", "digest", "radar"} and not PANDASCORE_API_TOKEN:
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

    if job == "radar":
        assert tournament_id is not None
        return _handle_radar_job(
            tournament_id,
            tournament_name,
            dry_run,
            test_run_id,
            radar_card_variant,
        )

    if job == "analytics":
        return _handle_analytics_job(event if isinstance(event, dict) else {}, dry_run)

    if job in {"schedule", "digest"}:
        return _handle_content_job(
            job,
            dry_run,
            test_run_id,
            include_filtered=include_filtered,
            days_ahead=days_ahead,
        )

    log_event(
        logger,
        logging.INFO,
        "handler_start",
        limit=limit,
        source=source,
        mode=mode,
        dry_run=dry_run,
        retry_only=retry_only,
    )
    rejected_matches: List[MatchNormalized] = []
    shadow_diagnostics: Dict[str, int] = {}
    if retry_only:
        matches: list[MatchNormalized] = []
    else:
        try:
            matches = asyncio.run(
                get_new_finished_matches(
                    limit=limit,
                    source=source,
                    dry_run=dry_run,
                    include_filtered=include_filtered,
                    check_processed=False,
                    rejected_matches=rejected_matches,
                    shadow_diagnostics=shadow_diagnostics,
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
    channels = list(_iter_channels())
    channels_by_id: dict[str, dict[str, Any]] = {}
    for channel in channels:
        name = str(channel.get("name", "unknown"))
        channel_id = str(channel.get("id") or name)
        channel_stats.setdefault(name, 0)
        channels_by_id[channel_id] = channel

    eligible_matches: list[MatchNormalized] = []
    for match in matches:
        if not isinstance(match, MatchNormalized):
            skipped_filtered += 1
            continue
        if not match.is_tier1_lan and not include_filtered:
            skipped_filtered += 1
            continue
        eligible_matches.append(match)

    if dry_run:
        for match in eligible_matches:
            for channel in channels:
                if not _match_matches_channel(match, channel.get("teams")):
                    continue
                name = str(channel.get("name", "unknown"))
                channel_stats[name] += 1
                sent_messages += 1
        pending_deliveries: list[PendingDelivery] = []
    else:
        current_targets: dict[str, PendingDelivery] = {}
        queued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for match in eligible_matches:
            for channel in channels:
                if not _match_matches_channel(match, channel.get("teams")):
                    continue
                name = str(channel.get("name", "unknown"))
                channel_id = str(channel.get("id") or name)
                key = result_outbox_key(match, channel_id)
                try:
                    if asyncio.run(
                        is_channel_processed(
                            match,
                            channel_id,
                            legacy_channel_name=name,
                        )
                    ):
                        skipped_duplicates += 1
                        continue
                    created = asyncio.run(enqueue_result_delivery(match, channel_id, name))
                    if created:
                        current_targets[key] = PendingDelivery(
                            key=key,
                            channel_id=channel_id,
                            channel_name=name,
                            match=match,
                            created_at=queued_at,
                        )
                except Exception as exc:
                    failed_messages += 1
                    log_event(
                        logger,
                        logging.ERROR,
                        "result_outbox_enqueue_failed",
                        channel=name,
                        match_uid=match.match_uid,
                        error_type=type(exc).__name__,
                        error=_safe_error_message(exc),
                    )

        try:
            stored_targets = asyncio.run(
                list_pending_result_deliveries(limit=RESULT_OUTBOX_LIMIT)
            )
        except Exception as exc:
            failed_messages += 1
            stored_targets = []
            log_event(
                logger,
                logging.ERROR,
                "result_outbox_load_failed",
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )
        for pending in stored_targets:
            current_targets[pending.key] = pending
        pending_deliveries = sorted(
            current_targets.values(),
            key=lambda item: (
                item.last_attempt_at is not None,
                item.last_attempt_at or item.created_at,
                item.created_at,
                item.key,
            ),
        )

    media_degraded = False
    if not dry_run and TELEGRAM_MEDIA_CARDS:
        try:
            media_degraded = asyncio.run(is_telegram_media_degraded())
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "telegram_media_health_read_failed",
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )
        if media_degraded:
            log_event(logger, logging.WARNING, "telegram_text_only_mode_active", job="results")

    for pending_index, pending in enumerate(pending_deliveries):
        elapsed = _monotonic() - handler_started_at
        if elapsed >= RESULT_DELIVERY_START_BUDGET_SECONDS:
            log_event(
                logger,
                logging.WARNING,
                "result_delivery_budget_exhausted",
                elapsed_seconds=round(elapsed, 3),
                remaining=len(pending_deliveries) - pending_index,
            )
            break

        match = pending.match
        channel = channels_by_id.get(pending.channel_id)
        if channel is None:
            try:
                asyncio.run(record_result_delivery_attempt(pending))
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "result_outbox_attempt_failed",
                    channel_id=pending.channel_id,
                    match_uid=match.match_uid,
                    error_type=type(exc).__name__,
                )
            log_event(
                logger,
                logging.WARNING,
                "result_outbox_channel_missing",
                channel_id=pending.channel_id,
                match_uid=match.match_uid,
            )
            continue
        name = str(channel.get("name", pending.channel_name))
        channel_id = pending.channel_id

        try:
            if asyncio.run(reconcile_channel_delivery(match, channel_id)):
                asyncio.run(delete_result_delivery(pending))
                log_event(
                    logger,
                    logging.INFO,
                    "delivery_state_reconciled",
                    channel=name,
                    match_uid=match.match_uid,
                )
                continue
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
            try:
                if asyncio.run(is_channel_processed(match, channel_id, legacy_channel_name=name)):
                    asyncio.run(delete_result_delivery(pending))
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "result_outbox_cleanup_failed",
                    channel=name,
                    match_uid=match.match_uid,
                    error_type=type(exc).__name__,
                )
            log_event(
                logger,
                logging.INFO,
                "duplicate_or_inflight_skipped",
                match_uid=match.match_uid,
                channel=name,
            )
            continue

        log_event(
            logger,
            logging.INFO,
            "delivery_claim_acquired",
            match_uid=match.match_uid,
            channel=name,
            outbox_attempt=pending.attempt_count + 1,
        )

        result_card: bytes | None = None
        if TELEGRAM_MEDIA_CARDS and not media_degraded:
            elapsed = _monotonic() - handler_started_at
            if elapsed >= RESULT_MEDIA_BUDGET_SECONDS:
                log_event(
                    logger,
                    logging.WARNING,
                    "media_card_budget_exhausted",
                    job="results",
                    match_uid=match.match_uid,
                    elapsed_seconds=round(elapsed, 3),
                )
            else:
                render_started_at = _monotonic()
                try:
                    result_card = (
                        render_final_card(match)
                        if match.source == "liquipedia" and can_render_final_card(match)
                        else render_result_card(match)
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "media_card_ready",
                        job="results",
                        match_uid=match.match_uid,
                        duration_seconds=round(_monotonic() - render_started_at, 3),
                    )
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

        telegram_confirmed = False
        delivery_format = "text"
        delivery_attempt: int | None = None
        try:
            text = format_match(match)
            response: dict[str, Any] | None = None
            if result_card is not None:
                try:
                    response = send_photo_to_telegram(
                        channel["chat_id"],
                        result_card,
                        text,
                        has_spoiler=TELEGRAM_SPOILERS,
                        filename=f"cs2-result-{match.match_id or 'match'}.png",
                        timeout=RESULT_TELEGRAM_TIMEOUT_SECONDS,
                        max_attempts=RESULT_TELEGRAM_MAX_ATTEMPTS,
                    )
                    delivery_format = "photo"
                    try:
                        asyncio.run(clear_telegram_media_degraded())
                    except Exception as health_exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "telegram_media_health_clear_failed",
                            error_type=type(health_exc).__name__,
                        )
                except TelegramDeliveryUncertainError:
                    raise
                except TelegramDeliveryError as exc:
                    if isinstance(exc, TelegramConnectTimeoutError):
                        media_degraded = True
                        try:
                            asyncio.run(
                                mark_telegram_media_degraded(
                                    datetime.now(timezone.utc)
                                    + timedelta(seconds=RESULT_MEDIA_DEGRADED_SECONDS)
                                )
                            )
                        except Exception as health_exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "telegram_media_health_write_failed",
                                error_type=type(health_exc).__name__,
                            )
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
                    response = send_to_telegram(
                        channel["chat_id"],
                        text,
                        timeout=RESULT_TELEGRAM_TIMEOUT_SECONDS,
                        max_attempts=RESULT_TEXT_TELEGRAM_MAX_ATTEMPTS,
                    )
                    delivery_format = "text"
            else:
                response = send_to_telegram(
                    channel["chat_id"],
                    text,
                    timeout=RESULT_TELEGRAM_TIMEOUT_SECONDS,
                    max_attempts=RESULT_TEXT_TELEGRAM_MAX_ATTEMPTS,
                )
            if isinstance(response, dict) and isinstance(response.get("_cs2results_attempt"), int):
                delivery_attempt = int(response["_cs2results_attempt"])
            telegram_confirmed = True
            claim = asyncio.run(mark_delivery_claim_sent(claim))
            channel_stats[name] += 1
            sent_messages += 1
            log_event(
                logger,
                logging.INFO,
                "telegram_delivery_succeeded",
                channel=name,
                match_uid=match.match_uid,
                delivery_format=delivery_format,
                telegram_attempt=delivery_attempt,
                outbox_attempt=pending.attempt_count + 1,
                media_card=delivery_format == "photo",
            )
        except Exception as exc:
            failed_messages += 1
            delivery_uncertain = isinstance(exc, TelegramDeliveryUncertainError)
            if delivery_uncertain:
                _notify_admin(
                    "telegram_delivery_uncertain",
                    f"Telegram не подтвердил исход доставки результата {match.match_uid} в канал {name}; повтор отключён.",
                )
            if not telegram_confirmed and not delivery_uncertain:
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
            try:
                asyncio.run(record_result_delivery_attempt(pending))
            except Exception as outbox_exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "result_outbox_attempt_failed",
                    channel=name,
                    match_uid=match.match_uid,
                    error_type=type(outbox_exc).__name__,
                )
            log_event(
                logger,
                logging.ERROR,
                "publish_failed",
                channel=name,
                match_uid=match.match_uid,
                outbox_attempt=pending.attempt_count + 1,
                error_type=type(exc).__name__,
                error=_safe_error_message(exc),
            )
            continue

        try:
            asyncio.run(mark_channel_processed(match, channel_id))
            asyncio.run(delete_result_delivery(pending))
            log_event(
                logger,
                logging.INFO,
                "delivery_marked_processed",
                channel=name,
                match_uid=match.match_uid,
            )
            _record_post_analytics(
                channel_id,
                match.match_uid,
                "results",
                tournament=match.tournament_name,
                media_card=delivery_format == "photo",
                delivery_format=delivery_format,
            )
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
        "retry_only": retry_only,
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
        "retry_only": retry_only,
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
        if shadow_diagnostics:
            body["liquipedia_shadow"] = shadow_diagnostics
    return {
        "statusCode": 502 if failed_messages else 200,
        "body": json.dumps(body),
    }
