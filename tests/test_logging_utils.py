import json
import logging

from cs2bot.logging_utils import log_event


def test_log_event_flushes_structured_payload_to_stdout(capsys):
    log_event(logging.getLogger("test.logging_utils"), logging.WARNING, "delivery_failed", count=2)

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "message": "delivery_failed",
        "level": "WARN",
        "stream_name": "cs2results",
        "event": "delivery_failed",
        "count": 2,
    }
