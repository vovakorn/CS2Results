import json
import logging

from cs2bot.logging_utils import log_event


def test_log_event_emits_yandex_structured_payload(caplog):
    with caplog.at_level(logging.WARNING, logger="test.logging_utils"):
        log_event(logging.getLogger("test.logging_utils"), logging.WARNING, "delivery_failed", count=2)

    assert json.loads(caplog.records[-1].message) == {
        "message": "delivery_failed",
        "level": "WARN",
        "stream_name": "cs2results",
        "event": "delivery_failed",
        "count": 2,
    }
