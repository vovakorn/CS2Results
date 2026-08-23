from __future__ import annotations

import json
import logging
import sys
from typing import Any


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    message = json.dumps(payload, ensure_ascii=False, default=str)

    # Yandex Cloud reliably ingests flushed stdout from Python functions, while
    # logger handlers can be replaced or dropped by the runtime. Keep the
    # logger call for local handlers/tests and emit the canonical event directly
    # so production diagnostics cannot disappear silently.
    logger.log(level, message)
    print(message, file=sys.stdout, flush=True)
