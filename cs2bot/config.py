import os
import json
from typing import Any

# Токен бота берём из переменных окружения Yandex Cloud.
# TELEGRAM_BOT_TOKEN оставлен как совместимое имя из README.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

# Канал по умолчанию — тоже из окружения (для v1 один канал)
DEFAULT_CHANNEL = os.getenv("TELEGRAM_CHAT_ID")
BOT_MODE = os.getenv("BOT_MODE", "production")


def _load_channels_from_env() -> list[dict[str, Any]] | None:
    raw = os.getenv("CHANNELS_JSON") or os.getenv("CHANNEL_CONFIG_JSON")
    if not raw:
        return None

    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("CHANNELS_JSON must be a JSON array")
    for channel in data:
        if not isinstance(channel, dict):
            raise ValueError("Each channel config entry must be an object")
    return data

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
