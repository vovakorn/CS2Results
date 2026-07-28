import json

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources.models import MapResult, MatchNormalized
from cs2bot.match_sources.storage import DeliveryClaim


@pytest.fixture(autouse=True)
def configured_runtime(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", "test-bucket")


def _match(match_id="1", team1="NAVI", team2="FaZe"):
    return MatchNormalized(
        source="cs2api",
        match_id=match_id,
        match_url=f"https://example.com/matches/{match_id}",
        tournament_name="IEM Cologne 2026",
        team1_name=team1,
        team2_name=team2,
        score1=2,
        score2=1,
        date="2026-02-17",
        is_tier1_lan=True,
    )


def _claim(match, channel_name):
    uid = f"{channel_name}_{match.match_uid}"
    return DeliveryClaim(uid, f"claims/{uid}.json", f"claim-{channel_name}", '"etag"')


def test_format_match_uses_normalized_fields():
    text = main.format_match(_match())
    assert "NAVI vs FaZe" in text
    assert "Score: 2:1" in text
    assert "Winner: NAVI" in text
    assert "Event: IEM Cologne 2026" in text
    assert "Match ID: 1" in text


def test_format_match_uses_display_timezone(monkeypatch):
    monkeypatch.setattr(main, "DISPLAY_TIMEZONE", "Europe/Berlin")
    match = _match()
    match.date = "2026-02-17T10:30:00Z"

    text = main.format_match(match)

    assert "Date: 2026-02-17 11:30 CET" in text


def test_format_match_prefers_end_date_and_includes_maps(monkeypatch):
    monkeypatch.setattr(main, "DISPLAY_TIMEZONE", "Europe/Berlin")
    match = _match()
    match.start_date = "2026-02-17T10:30:00Z"
    match.end_date = "2026-02-17T12:40:00Z"
    match.maps = [
        MapResult(name="Mirage", score1=13, score2=11),
        MapResult(name="Ancient", score1=7, score2=13),
    ]

    text = main.format_match(match)

    assert "Date: 2026-02-17 13:40 CET" in text
    assert "Maps: Mirage 13:11, Ancient 7:13" in text


def test_format_match_drops_untrusted_source_url():
    match = _match()
    match.match_url = "https://attacker.example/phishing"

    text = main.format_match(match)

    assert "attacker.example" not in text


def test_format_match_allows_expected_source_url():
    match = _match()
    match.match_url = "https://bo3.gg/matches/1"

    assert "https://bo3.gg/matches/1" in main.format_match(match)


def test_format_match_caps_telegram_message_length():
    match = _match(team1="N" * 200, team2="F" * 200)
    match.tournament_name = "IEM " + ("X" * 296)
    match.location = "Y" * 300
    match.maps = [MapResult(name="M" * 100, score1=13, score2=11) for _ in range(10)]

    assert len(main.format_match(match)) <= main.MAX_TELEGRAM_MESSAGE_LENGTH


def test_handler_dry_run_does_not_send_or_mark(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is True
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))

    async def fake_mark(match, channel_name):
        marked.append(match.match_uid)

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1, "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_received"] == 1
    assert body["messages_sent"] == 1
    assert sent == []
    assert marked == []


def test_handler_marks_processed_after_successful_send(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is False
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))
        return {"ok": True}

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_mark(match, channel_name):
        marked.append((match.match_uid, channel_name))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 1
    assert len(sent) == 1
    assert marked == [(_match().match_uid, "global")]


def test_handler_skips_channel_duplicate(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["check_processed"] is False
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return None

    async def fake_mark(match, channel_name):
        marked.append((match.match_uid, channel_name))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 0
    assert body["duplicates_skipped"] == 1
    assert sent == []
    assert marked == []


def test_handler_does_not_mark_when_send_fails(monkeypatch):
    marked = []
    released = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        raise RuntimeError("telegram failed")

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_mark(match, channel_name):
        marked.append(match.match_uid)

    async def fake_release(claim):
        released.append(claim.match_uid)

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 502
    assert marked == []
    assert released == [f"global_{_match().match_uid}"]


def test_handler_marks_successful_channel_before_later_channel_fails(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append(chat_id)
        if chat_id == "chat-b":
            raise RuntimeError("telegram failed")
        return {"ok": True}

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_mark(match, channel_name):
        marked.append((match.match_uid, channel_name))

    async def fake_release(claim):
        return None

    monkeypatch.setattr(
        main,
        "CHANNELS",
        [
            {"name": "a", "chat_id": "chat-a", "teams": None},
            {"name": "b", "chat_id": "chat-b", "teams": None},
        ],
    )
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 502
    assert sent == ["chat-a", "chat-b"]
    assert marked == [(_match().match_uid, "a")]


def test_debug_mode_cannot_publish_filtered_matches(monkeypatch):
    filtered = _match()
    filtered.is_tier1_lan = False
    filtered.filter_reason = "lan_unconfirmed"
    sent = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["include_filtered"] is False
        return [filtered]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)

    response = main.handler({"mode": "debug", "include_filtered": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["filtered_skipped"] == 1
    assert sent == []


def test_handler_uses_match_source_from_environment_default(monkeypatch):
    seen = {}

    async def fake_get_new_finished_matches(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(main, "MATCH_SOURCE", "hltv")
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"dry_run": True}, None)

    assert response["statusCode"] == 200
    assert seen["source"] == "hltv"


def test_telegram_http_error_never_exposes_token(monkeypatch):
    class FakeResponse:
        status_code = 400

        def json(self):
            return {"ok": False, "description": "bad request"}

    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:SECRET")
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(main.TelegramDeliveryError) as exc_info:
        main.send_to_telegram("chat", "text", max_attempts=1)

    assert "SECRET" not in str(exc_info.value)
    assert "api.telegram.org" not in str(exc_info.value)


def test_telegram_redirect_is_not_followed(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 302

        def json(self):
            return {}

    def fake_post(*args, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    with pytest.raises(main.TelegramDeliveryError):
        main.send_to_telegram("chat", "text", max_attempts=1)

    assert seen["allow_redirects"] is False


def test_telegram_network_exception_never_exposes_token(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:SECRET")

    def fail(*args, **kwargs):
        raise requests.ConnectionError(
            "failed for https://api.telegram.org/bot123456:SECRET/sendMessage"
        )

    monkeypatch.setattr(main.requests, "post", fail)

    with pytest.raises(main.TelegramDeliveryError) as exc_info:
        main.send_to_telegram("chat", "text", max_attempts=1)

    assert "SECRET" not in str(exc_info.value)


def test_handler_returns_generic_fetch_error_and_redacts_logs(monkeypatch, caplog):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:SECRET")

    async def fail(**kwargs):
        raise RuntimeError("https://api.telegram.org/bot123456:SECRET/sendMessage")

    monkeypatch.setattr(main, "get_new_finished_matches", fail)

    response = main.handler({"dry_run": True}, None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {"error": "match_source_unavailable"}
    assert "SECRET" not in caplog.text


def test_invalid_dry_run_value_cannot_fall_through_to_production(monkeypatch):
    called = False

    async def fake_get_new_finished_matches(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"dry_run": "tru"}, None)

    assert response["statusCode"] == 400
    assert called is False


def test_missing_runtime_configuration_fails_before_fetch(monkeypatch):
    called = False

    async def fake_get_new_finished_matches(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "TELEGRAM_TOKEN", None)
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", None)
    monkeypatch.setattr(main, "CHANNELS", [])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({}, None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {"error": "configuration_error"}
    assert called is False
