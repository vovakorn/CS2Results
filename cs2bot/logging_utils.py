from __future__ import annotations

import json
import logging
from typing import Any


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    level_name = logging.getLevelName(level)
    if level_name == "WARNING":
        level_name = "WARN"
    payload = {
        "message": event,
        "level": level_name,
        "stream_name": "cs2results",
        "event": event,
        **fields,
    }
    message = json.dumps(payload, ensure_ascii=False, default=str)

    # Yandex Cloud's Python runtime provides a stdout handler for the root
    # logger. Using its structured JSON message keeps events searchable without
    # writing each event twice through a second direct stream.
    logger.log(level, message)
