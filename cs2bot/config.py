import os
import json
import re
from typing import Any

# Токен бота берём из переменных окружения Yandex Cloud.
# TELEGRAM_BOT_TOKEN оставлен как совместимое имя из README.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

# Канал по умолчанию — тоже из окружения (для v1 один канал)
DEFAULT_CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
BOT_MODE = os.getenv("BOT_MODE", "production")


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


TELEGRAM_SPOILERS = _bool_env("TELEGRAM_SPOILERS", True)
TELEGRAM_MEDIA_CARDS = _bool_env("TELEGRAM_MEDIA_CARDS", False)
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")


def _load_channels_from_env() -> list[dict[str, Any]] | None:
    raw = os.getenv("CHANNELS_JSON") or os.getenv("CHANNEL_CONFIG_JSON")
    if not raw:
        return None

    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("CHANNELS_JSON must be a JSON array")
    if not data or len(data) > 50:
        raise ValueError("CHANNELS_JSON must contain between 1 and 50 channels")
    normalized_channels: list[dict[str, Any]] = []
    storage_ids: set[str] = set()
    for channel in data:
        if not isinstance(channel, dict):
            raise ValueError("Each channel config entry must be an object")
        name = channel.get("name")
        chat_id = channel.get("chat_id")
        teams = channel.get("teams")
        storage_id = channel.get("id", name)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Each channel must have a non-empty string name")
        if not isinstance(storage_id, str) or not storage_id.strip() or len(storage_id.strip()) > 100:
            raise ValueError("Each channel id must be a non-empty string")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, (str, int))
            or str(chat_id).strip() == ""
            or len(str(chat_id)) > 200
        ):
            raise ValueError(f"Channel {name!r} must have a chat_id")
        if teams is not None and (
            not isinstance(teams, list)
            or len(teams) > 100
            or not all(isinstance(team, str) and team.strip() for team in teams)
            or any(len(team.strip()) > 200 for team in teams if isinstance(team, str))
        ):
            raise ValueError(f"Channel {name!r} teams must be null or a string array")

        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", storage_id.strip()).strip("_") or "unknown"
        if safe_id in storage_ids:
            raise ValueError("Channel ids must remain unique after storage normalization")
        storage_ids.add(safe_id)
        normalized_channels.append(
            {
                "id": storage_id.strip(),
                "name": name.strip(),
                "chat_id": chat_id,
                "teams": [team.strip() for team in teams] if teams else None,
            }
        )
    return normalized_channels

# Конфигурация каналов.
# Сейчас у нас один канал, который получает ВСЕ матчи.
# В будущем сюда можно добавить до 50 каналов для разных команд.
CHANNELS = _load_channels_from_env() or [
    {
        "name": "global",           # просто метка для тебя
        "chat_id": DEFAULT_CHANNEL, # chat_id или @username канала
        "teams": None,              # None = без фильтра по командам
    },
    # Пример на будущее:
    # {
    #     "name": "NAVI",
    #     "chat_id": "@navi_results",
    #     "teams": ["Natus Vincere", "NaVi"],
    # },
]
