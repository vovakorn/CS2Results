"""Private Telegram control room.  It accepts only owner webhook updates."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import (CHANNELS, PUBLICATION_CONTROL_ENABLED, TELEGRAM_ADMIN_ENABLED,
                     TELEGRAM_ADMIN_USER_ID, TELEGRAM_ADMIN_WEBHOOK_SECRET,
                     TELEGRAM_PROXY_URL, TELEGRAM_TOKEN)
from .match_sources.storage import list_pending_result_deliveries
from .publication_admin import (PUBLICATION_TYPES, list_publications, pending_delivery_status,
                                policy_status, publications_in_period, queue_counts, read_policy,
                                update_policy)

MOSCOW = ZoneInfo("Europe/Moscow")
SECRET_HEADER = "x-telegram-bot-api-secret-token"
TELEGRAM_API_URL = "https://api.telegram.org"


def _error(status: int) -> dict[str, Any]:
    return {"statusCode": status, "body": json.dumps({"ok": False})}


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}


def _update(event: dict[str, Any]) -> dict[str, Any] | None:
    body = event.get("body", event)
    if isinstance(body, str):
        try: body = json.loads(body)
        except json.JSONDecodeError: return None
    return body if isinstance(body, dict) else None


def _owner_update(event: dict[str, Any]) -> dict[str, Any] | None:
    if not TELEGRAM_ADMIN_ENABLED or not TELEGRAM_ADMIN_USER_ID or not TELEGRAM_ADMIN_WEBHOOK_SECRET:
        return None
    if not hmac.compare_digest(_headers(event).get(SECRET_HEADER, ""), TELEGRAM_ADMIN_WEBHOOK_SECRET):
        return None
    update = _update(event)
    if not update: return None
    source = update.get("callback_query") or update.get("message")
    if not isinstance(source, dict): return None
    sender = source.get("from") or {}
    chat = (source.get("message") or source).get("chat") or {}
    if not isinstance(sender.get("id"), int) or not hmac.compare_digest(str(sender["id"]), str(TELEGRAM_ADMIN_USER_ID)):
        return None
    if chat.get("type") != "private": return None
    return update


def telegram_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Small, replaceable Bot API adapter; no endpoint or secret is logged."""
    if not TELEGRAM_TOKEN: raise RuntimeError("telegram credentials are not configured")
    options: dict[str, Any] = {"json": payload, "timeout": 7, "allow_redirects": False}
    if TELEGRAM_PROXY_URL: options["proxies"] = {"http": TELEGRAM_PROXY_URL, "https": TELEGRAM_PROXY_URL}
    response = requests.post(f"{TELEGRAM_API_URL}/bot{TELEGRAM_TOKEN}/{method}", **options)
    if response.status_code >= 300: raise RuntimeError("telegram admin response unavailable")
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True: raise RuntimeError("telegram admin response invalid")
    return data


def _destinations() -> list[tuple[str, str, str]]:
    telegram = [(str(item.get("id") or item.get("name")), str(item.get("name") or "Telegram"), "telegram")
                for item in CHANNELS if item.get("chat_id")]
    return telegram + [("instagram", "Instagram", "instagram"), ("threads", "Threads", "threads")]


def _keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def _render(view: str, policy, *, dest_index: int = 0, type_index: int = 0, period: str = "today", notice: str = "") -> tuple[str, dict[str, Any]]:
    destinations, types = _destinations(), list(PUBLICATION_TYPES)
    stamp = datetime.now(MOSCOW).strftime("%d.%m %H:%M МСК")
    suffix = f"\n\n<i>{notice}</i>" if notice else ""
    if view == "home":
        text = f"<b>CS2 Results · админка</b>\nОбновлено: {stamp}{suffix}"
        keys = [[("Обзор", "a:o"), ("Каналы", "a:c")], [("Типы постов", "a:f"), ("Журнал", "a:j")], [("Проблемы", "a:p")]]
    elif view == "channels":
        text = "<b>Каналы и аккаунты</b>\nВыберите назначение."
        keys = [[(name, f"a:d:{index}")] for index, (_, name, _) in enumerate(destinations)] + [[("Главное меню", "a:h")]]
    elif view == "formats":
        text = "<b>Типы постов</b>\nВыберите формат."
        keys = [[(PUBLICATION_TYPES[key][0], f"a:f:{index}")] for index, key in enumerate(types)] + [[("Главное меню", "a:h")]]
    elif view == "destination" and 0 <= dest_index < len(destinations):
        dest_id, name, platform = destinations[dest_index]
        text = f"<b>{name}</b>\nВыберите формат."
        keys = []
        for index, key in enumerate(types):
            supported = platform in PUBLICATION_TYPES[key][1]
            enabled, _ = policy_status(policy, dest_id, key)
            label = "✓" if enabled else "⏸"
            keys.append([(f"{label} {PUBLICATION_TYPES[key][0]}" if supported else f"— {PUBLICATION_TYPES[key][0]}", f"a:x:{dest_index}:{index}")])
        keys.append([("Назад", "a:c"), ("Главное меню", "a:h")])
    elif view == "format" and 0 <= type_index < len(types):
        key = types[type_index]
        text = f"<b>{PUBLICATION_TYPES[key][0]}</b>\nВыберите площадку."
        keys = [[(name, f"a:x:{index}:{type_index}")] for index, (_, name, platform) in enumerate(destinations) if platform in PUBLICATION_TYPES[key][1]]
        keys.append([("Назад", "a:f"), ("Главное меню", "a:h")])
    elif view == "detail" and dest_index < len(destinations) and type_index < len(types):
        dest_id, name, platform = destinations[dest_index]; key = types[type_index]; label, supported = PUBLICATION_TYPES[key][0], platform in PUBLICATION_TYPES[key][1]
        if not supported:
            text = f"<b>{label}</b>\n{name}\n\nЭтот формат не реализован для выбранной площадки."
            keys = [[("Назад", f"a:d:{dest_index}"), ("Главное меню", "a:h")]]
        else:
            enabled, generation = policy_status(policy, dest_id, key)
            setting = "включено" if enabled else "выключено"
            control = "активен" if PUBLICATION_CONTROL_ENABLED else "будет активен после подключения control-флага"
            text = (f"<b>{label}</b>\n{name}\n\nНастройка владельца: <b>{setting}</b>\n"
                    f"Поколение очереди: {generation}\nКонтроль доставки: {control}\n\n"
                    "История до включения новой версии может быть неполной.")
            action = "Выключить" if enabled else "Включить"
            keys = [[(action, f"a:t:{dest_index}:{type_index}:{policy.revision}:{int(not enabled)}")], [("Назад", f"a:d:{dest_index}"), ("Главное меню", "a:h")]]
    elif view == "confirm":
        text = notice
        keys = []
    else:
        text = f"<b>Обзор · {stamp}</b>\nЖурнал подтверждённых публикаций ведётся с включения этой версии. История до него может быть неполной."
        keys = [[("Сегодня", "a:o"), ("Журнал", "a:j")], [("Главное меню", "a:h")]]
    return text, _keyboard(keys)


def _send_or_edit(update: dict[str, Any], text: str, markup: dict[str, Any]) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        message = callback.get("message") or {}
        telegram_call("editMessageText", {"chat_id": message["chat"]["id"], "message_id": message["message_id"], "text": text, "parse_mode": "HTML", "reply_markup": markup, "disable_web_page_preview": True})
    else:
        message = update["message"]
        telegram_call("sendMessage", {"chat_id": message["chat"]["id"], "text": text, "parse_mode": "HTML", "reply_markup": markup, "disable_web_page_preview": True})


PERIODS = {"today": "Сегодня", "week": "7 дней", "month": "Месяц"}
STATUS_LABELS = {"sent": "опубликовано", "pending": "ожидает отправки", "retry_safe": "ожидает повтора",
                 "suspended": "приостановлено", "manual_review": "ручная проверка", "uncertain": "исход не подтверждён",
                 "error": "ошибка", "skipped": "пропущено"}


def _overview(records: list[dict[str, Any]], pending: list[Any], policy: Any, period: str) -> str:
    visible = publications_in_period(records, period)
    destinations: dict[str, int] = {}
    formats: dict[str, int] = {}
    for item in visible:
        destinations[item["destination_id"]] = destinations.get(item["destination_id"], 0) + 1
        formats[item["publication_type"]] = formats.get(item["publication_type"], 0) + 1
    destination_text = ", ".join(f"{key}: {value}" for key, value in sorted(destinations.items())) or "нет"
    format_text = ", ".join(f"{key}: {value}" for key, value in sorted(formats.items())) or "нет"
    queue = queue_counts(pending, policy)
    queue_text = (f"ожидают: {queue['pending']}; повтор: {queue['retry_safe']}; "
                  f"приостановлены: {queue['suspended']}; ручная проверка: {queue['manual_review']}")
    return (f"<b>Обзор · {PERIODS[period].lower()}</b>\nПодтверждённые публикации: <b>{len(visible)}</b>\n"
            f"По назначениям: {destination_text}\nПо форматам: {format_text}\n\n"
            f"Очередь сейчас: {queue_text}.\nИстория до включения новой версии неполная.")


def _journal_text(records: list[dict[str, Any]], pending: list[Any], policy: Any, period: str, page: int) -> str:
    confirmed = publications_in_period(records, period)
    rows: list[tuple[str, str]] = []
    for item in confirmed:
        rows.append((str(item.get("confirmed_at", "—")),
                     f"{item.get('publication_type', '—')} · {item.get('destination_id', '—')} · опубликовано"))
    for item in pending:
        status = pending_delivery_status(item, policy)
        rows.append((str(getattr(item, "created_at", "—")),
                     f"{getattr(item, 'content_type', 'result')} · {getattr(item, 'channel_name', '—')} · {STATUS_LABELS[status]}"))
    rows.sort(reverse=True)
    start = max(0, page) * 10
    page_rows = rows[start:start + 10]
    return "<b>Журнал · " + PERIODS[period].lower() + "</b>\n" + ("\n".join(f"{when} · {detail}" for when, detail in page_rows) if page_rows else "Записей нет.")


def _pending() -> list[Any]:
    try:
        return asyncio.run(list_pending_result_deliveries(limit=200))
    except Exception:
        return []


def _pair_detail(records: list[dict[str, Any]], pending: list[Any], policy: Any,
                 destination_id: str, publication_type: str, period: str = "today") -> str:
    confirmed = [item for item in records if not item.get("test")
                 and item.get("destination_id") == destination_id
                 and item.get("publication_type") == publication_type]
    in_period = publications_in_period(confirmed, period)
    last = str(confirmed[0].get("confirmed_at")) if confirmed else "нет подтверждённой публикации"
    related = [item for item in pending
               if str(getattr(item, "channel_id", "")) == destination_id
               and {"result": "results"}.get(str(getattr(item, "content_type", "result")), str(getattr(item, "content_type", "result"))) == publication_type]
    queue = ", ".join(STATUS_LABELS[pending_delivery_status(item, policy)] for item in related) or "нет"
    problem = next((STATUS_LABELS[pending_delivery_status(item, policy)] for item in related
                    if pending_delivery_status(item, policy) in {"suspended", "manual_review"}), "нет")
    return (f"\n\nПоследняя публикация: {last}\nЗа {PERIODS[period].lower()}: {len(in_period)}\n"
            f"Очередь: {queue}\nПоследняя существенная проблема: {problem}")


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    update = _owner_update(event or {})
    if update is None: return _error(403)
    callback = update.get("callback_query")
    data = callback.get("data", "") if isinstance(callback, dict) else "a:h"
    message = update.get("message") if isinstance(update.get("message"), dict) else None
    if message and message.get("text") != "/admin": return {"statusCode": 200, "body": json.dumps({"ok": True})}
    try:
        policy = asyncio.run(read_policy())
        parts = data.split(":")
        if parts[0] != "a": raise ValueError
        view, kwargs = "home", {}
        if parts[1] == "c": view = "channels"
        elif parts[1] == "f": view, kwargs = ("formats", {}) if len(parts) == 2 else ("format", {"type_index": int(parts[2])})
        elif parts[1] == "d": view, kwargs = "destination", {"dest_index": int(parts[2])}
        elif parts[1] == "x":
            dest_index, type_index = int(parts[2]), int(parts[3])
            destinations, types = _destinations(), list(PUBLICATION_TYPES)
            text, markup = _render("detail", policy, dest_index=dest_index, type_index=type_index)
            if 0 <= dest_index < len(destinations) and 0 <= type_index < len(types):
                destination_id, _, platform = destinations[dest_index]
                publication_type = types[type_index]
                if platform in PUBLICATION_TYPES[publication_type][1]:
                    text += _pair_detail(asyncio.run(list_publications(limit=500)), _pending(), policy,
                                         destination_id, publication_type)
            _send_or_edit(update, text, markup)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        elif parts[1] == "t":
            dest_index, type_index, revision, enabled = map(int, parts[2:6])
            destinations, types = _destinations(), list(PUBLICATION_TYPES)
            dest_id, destination_name, _ = destinations[dest_index]
            publication_type, publication_name = types[type_index], PUBLICATION_TYPES[types[type_index]][0]
            operation = "включить" if enabled else "выключить"
            consequence = ("Новые выпуски будут разрешены; старая очередь останется на ручной проверке."
                           if enabled else "Новые отправки и ещё не начатая очередь будут приостановлены.")
            text = (f"<b>Подтвердите действие</b>\n{publication_name} · {destination_name}\n\n"
                    f"{operation.capitalize()}? {consequence}")
            markup = _keyboard([[("Подтвердить", f"a:q:{dest_index}:{type_index}:{revision}:{enabled}")],
                                [("Отмена", f"a:x:{dest_index}:{type_index}")]])
            _send_or_edit(update, text, markup)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        elif parts[1] == "q":
            dest_index, type_index, revision, enabled = map(int, parts[2:6])
            destinations, types = _destinations(), list(PUBLICATION_TYPES)
            dest_id = destinations[dest_index][0]; key = types[type_index]
            updated = asyncio.run(update_policy(dest_id, key, bool(enabled), actor_user_id=int(TELEGRAM_ADMIN_USER_ID), operation_id=secrets.token_hex(8), expected_revision=revision))
            text, markup = _render("detail", updated, dest_index=dest_index, type_index=type_index, notice="Настройка сохранена.")
            _send_or_edit(update, text, markup)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        elif parts[1] == "o":
            period = parts[2] if len(parts) > 2 and parts[2] in PERIODS else "today"
            records = asyncio.run(list_publications(limit=500))
            _send_or_edit(update, _overview(records, _pending(), policy, period), _keyboard([
                [(label, f"a:o:{key}") for key, label in PERIODS.items()],
                [('Журнал', f'a:j:0:{period}')], [('Главное меню', 'a:h')]]))
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        elif parts[1] == "j":
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            period = parts[3] if len(parts) > 3 and parts[3] in PERIODS else "today"
            text = _journal_text(asyncio.run(list_publications(limit=500)), _pending(), policy, period, page)
            navigation = [("←", f"a:j:{max(0, page - 1)}:{period}"), ("→", f"a:j:{page + 1}:{period}")]
            _send_or_edit(update, text, _keyboard([navigation, [('Обзор', f'a:o:{period}')], [('Главное меню', 'a:h')]]))
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        elif parts[1] == "p":
            pending = _pending()
            problems = [item for item in pending if pending_delivery_status(item, policy) in {"suspended", "manual_review"}]
            if problems:
                details = "\n".join(
                    f"{getattr(item, 'content_type', 'result')} · {getattr(item, 'channel_name', '—')} · "
                    f"{STATUS_LABELS[pending_delivery_status(item, policy)]}" for item in problems[:10]
                )
                text = "<b>Проблемы</b>\n" + details
            else:
                text = "<b>Проблемы</b>\nНет удержанных доставок в доступной очереди. Неопределённые исходы требуют отдельного claim-индекса."
            markup = _keyboard([[('Журнал', 'a:j:0:today')], [('Главное меню', 'a:h')]])
            _send_or_edit(update, text, markup)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        else: view = "home"
        text, markup = _render(view, policy, **kwargs)
        _send_or_edit(update, text, markup)
    except (ValueError, IndexError):
        text, markup = _render("home", asyncio.run(read_policy()), notice="Экран устарел. Откройте меню заново.")
        _send_or_edit(update, text, markup)
    except Exception:
        return _error(503)
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
