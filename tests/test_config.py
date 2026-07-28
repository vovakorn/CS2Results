import json

import pytest

from cs2bot.config import _load_channels_from_env
from cs2bot.match_sources.config import _load_json_file


def test_load_json_file_reads_tier1_config(tmp_path):
    config_path = tmp_path / "tier1.json"
    config_path.write_text(
        json.dumps({"tournament_patterns": ["Test Masters"], "prize_pool_threshold_usd": 100}),
        encoding="utf-8",
    )

    data = _load_json_file(str(config_path))

    assert data["tournament_patterns"] == ["Test Masters"]
    assert data["prize_pool_threshold_usd"] == 100


def test_load_json_file_ignores_missing_file(tmp_path):
    assert _load_json_file(str(tmp_path / "missing.json")) == {}


def test_channel_config_is_normalized_and_validated(monkeypatch):
    monkeypatch.setenv(
        "CHANNELS_JSON",
        json.dumps(
            [
                {
                    "id": "stable-global",
                    "name": " Global ",
                    "chat_id": "@channel",
                    "teams": [" NAVI "],
                }
            ]
        ),
    )

    channels = _load_channels_from_env()

    assert channels == [
        {
            "id": "stable-global",
            "name": "Global",
            "chat_id": "@channel",
            "teams": ["NAVI"],
        }
    ]


def test_channel_config_rejects_storage_id_collisions(monkeypatch):
    monkeypatch.setenv(
        "CHANNELS_JSON",
        json.dumps(
            [
                {"id": "a/b", "name": "one", "chat_id": "1"},
                {"id": "a b", "name": "two", "chat_id": "2"},
            ]
        ),
    )

    with pytest.raises(ValueError, match="unique"):
        _load_channels_from_env()


def test_channel_config_rejects_string_instead_of_team_array(monkeypatch):
    monkeypatch.setenv(
        "CHANNELS_JSON",
        json.dumps([{"name": "global", "chat_id": "1", "teams": "NAVI"}]),
    )

    with pytest.raises(ValueError, match="string array"):
        _load_channels_from_env()


def test_channel_config_rejects_boolean_chat_id(monkeypatch):
    monkeypatch.setenv(
        "CHANNELS_JSON",
        json.dumps([{"name": "global", "chat_id": True}]),
    )

    with pytest.raises(ValueError, match="chat_id"):
        _load_channels_from_env()
