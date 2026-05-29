import json

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
