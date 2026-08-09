import json

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources.models import MapResult, MatchNormalized, UpcomingMatchNormalized
from cs2bot.match_sources.storage import DeliveryClaim


@pytest.fixture(autouse=True)
def configured_runtime(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", "pandascore-token")


def _match(match_id="1", team1="NAVI", team2="FaZe"):
    return MatchNormalized(
        source="pandascore",
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


def _upcoming():
    return UpcomingMatchNormalized(
        match_id="upcoming-1",
        tournament_name="IEM Cologne 2026",
        team1_name="NAVI",
        team2_name="FaZe",
        scheduled_at="2026-07-30T11:00:00Z",
        best_of=3,
        is_featured=True,
        feature_reason="tier1_tournament",
    )


def test_format_match_uses_normalized_fields():
    text = main.format_match(_match())
    assert "<b>NAVI</b>  <tg-spoiler>2 : 1</tg-spoiler>  <b>FAZE</b>" in text
    assert "Победитель: <tg-spoiler><b>NAVI</b></tg-spoiler>" in text
    assert "<b>IEM COLOGNE 2026</b>" in text
    assert "Match ID" not in text
    assert "PandaScore · #CS2 #РезультатыМатчей" in text
    assert "🏆" not in text
    assert "⚔️" not in text
    assert "📊" not in text
    assert "✅" not in text


def test_format_match_omits_match_time():
    match = _match()
    match.date = "2026-02-17T10:30:00Z"

    text = main.format_match(match)

    assert "Дата:" not in text
    assert "10:30" not in text


def test_format_match_includes_maps_and_omits_time():
    match = _match()
    match.start_date = "2026-02-17T10:30:00Z"
    match.end_date = "2026-02-17T12:40:00Z"
    match.maps = [
        MapResult(name="Mirage", score1=13, score2=11),
        MapResult(name="Ancient", score1=7, score2=13),
    ]

    text = main.format_match(match)

    assert "Дата:" not in text
    assert "12:40" not in text
    assert "Карты — Mirage 13:11 · Ancient 7:13" in text


def test_format_match_drops_untrusted_source_url():
    match = _match()
    match.match_url = "https://attacker.example/phishing"

    text = main.format_match(match)

    assert "attacker.example" not in text


def test_format_match_allows_expected_source_url():
    match = _match()
    match.source = "liquipedia"
    match.match_url = "https://liquipedia.net/counterstrike/IEM_Cologne/Matches"

    assert match.match_url in main.format_match(match)
    assert '<a href="https://liquipedia.net/' in main.format_match(match)


def test_format_match_escapes_untrusted_html():
    match = _match(team1="<b>Fake</b>")
    match.tournament_name = "IEM <script>alert(1)</script>"

    text = main.format_match(match)

    assert "<script>" not in text
    assert "&lt;SCRIPT&gt;" in text
    assert "&lt;B&gt;FAKE&lt;/B&gt;" in text


def test_format_match_can_disable_spoilers(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_SPOILERS", False)

    text = main.format_match(_match())

    assert "<tg-spoiler>" not in text
    assert "<b>NAVI</b>  2 : 1  <b>FAZE</b>" in text


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
    assert body["diagnostics"] == [
        {
            "source": "pandascore",
            "match_id": "1",
            "tournament": "IEM Cologne 2026",
            "competition_key": None,
            "source_refs": None,
            "tournament_tier": None,
            "teams": ["NAVI", "FaZe"],
            "score": [2, 1],
            "date": "2026-02-17",
            "start_date": None,
            "end_date": None,
            "original_scheduled_at": None,
            "rescheduled": None,
            "forfeit": None,
            "is_lan": None,
            "location": None,
            "is_tier1_lan": True,
            "filter_reason": None,
        }
    ]
    assert sent == []
    assert marked == []


def test_result_uses_spoiler_photo_when_media_cards_enabled(monkeypatch):
    sent_photos = []
    sent_text = []
    match = _match()

    async def fake_get_new_finished_matches(**kwargs):
        return [match]

    async def fake_claim(*args, **kwargs):
        return _claim(match, "global")

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "render_result_card", lambda item: b"card")
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent_text.append(args))
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: sent_photos.append((args, kwargs)),
    )

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 200
    assert sent_text == []
    assert sent_photos[0][0][1] == b"card"
    assert sent_photos[0][1]["has_spoiler"] is True
    assert "<tg-spoiler>2 : 1</tg-spoiler>" in sent_photos[0][0][2]


def test_result_falls_back_to_text_when_photo_delivery_fails(monkeypatch):
    sent_text = []
    match = _match()

    async def fake_get_new_finished_matches(**kwargs):
        return [match]

    async def fake_claim(*args, **kwargs):
        return _claim(match, "global")

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "render_result_card", lambda item: b"card")
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: (_ for _ in ()).throw(main.TelegramDeliveryError("failed")),
    )
    monkeypatch.setattr(
        main,
        "send_to_telegram",
        lambda chat_id, text, **kwargs: sent_text.append((chat_id, text)),
    )

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 200
    assert sent_text[0][0] == "chat"
    assert "<b>NAVI</b>  <tg-spoiler>2 : 1</tg-spoiler>  <b>FAZE</b>" in sent_text[0][1]


def test_handler_dry_run_reports_rejected_match_diagnostics(monkeypatch):
    rejected = _match(team1="Liquid", team2="Spirit")
    rejected.tournament_name = "BLAST Bounty 2026 Season 2"
    rejected.is_tier1_lan = False
    rejected.filter_reason = "lan_unconfirmed"

    async def fake_get_new_finished_matches(**kwargs):
        kwargs["rejected_matches"].append(rejected)
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"limit": 30, "dry_run": True}, None)
    body = json.loads(response["body"])

    assert body["matches_received"] == 0
    assert body["tier1_lan_unconfirmed"] == 1
    assert body["diagnostics"][0]["tournament"] == "BLAST Bounty 2026 Season 2"
    assert body["diagnostics"][0]["teams"] == ["Liquid", "Spirit"]
    assert body["diagnostics"][0]["filter_reason"] == "lan_unconfirmed"


def test_handler_dry_run_prioritizes_unconfirmed_tier1_diagnostics(monkeypatch):
    ordinary = [
        _match(match_id=str(index), team1=f"Local {index}", team2=f"Regional {index}")
        for index in range(main.MAX_MATCHES)
    ]
    for match in ordinary:
        match.tournament_name = "Regional League"
        match.is_tier1_lan = False
        match.filter_reason = "lan_unconfirmed"

    tier1 = _match(match_id="tier1", team1="Liquid", team2="Spirit")
    tier1.tournament_name = "BLAST Bounty — 2026 Season 2 — Playoffs"
    tier1.is_tier1_lan = False
    tier1.filter_reason = "lan_unconfirmed"

    async def fake_get_new_finished_matches(**kwargs):
        kwargs["rejected_matches"].extend([*ordinary, tier1])
        return ordinary

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"dry_run": True, "include_filtered": True}, None)
    body = json.loads(response["body"])

    assert len(body["diagnostics"]) == main.MAX_MATCHES
    assert body["diagnostics"][0]["match_id"] == "tier1"
    assert body["diagnostics"][0]["teams"] == ["Liquid", "Spirit"]


def test_handler_production_response_omits_match_diagnostics(monkeypatch):
    async def fake_get_new_finished_matches(**kwargs):
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({}, None)
    body = json.loads(response["body"])

    assert "diagnostics" not in body


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


def test_handler_alerts_admin_when_tier1_match_has_no_lan_evidence(monkeypatch):
    rejected = _match(team1="Liquid", team2="Spirit")
    rejected.tournament_name = "BLAST Bounty — 2026 Season 3"
    rejected.is_tier1_lan = False
    rejected.filter_reason = "lan_unconfirmed"
    alerts = []

    async def fake_get_new_finished_matches(**kwargs):
        kwargs["rejected_matches"].append(rejected)
        return []

    def fake_notify(alert_code, message):
        alerts.append((alert_code, message))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "_notify_admin", fake_notify)

    response = main.handler({"limit": 30}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["tier1_lan_unconfirmed"] == 1
    assert alerts[0][0] == "tier1_lan_unconfirmed"
    assert "Liquid — Spirit" in alerts[0][1]


def test_handler_does_not_alert_for_non_tier1_lan_uncertainty(monkeypatch):
    rejected = _match(team1="Local One", team2="Local Two")
    rejected.tournament_name = "Regional Finals"
    rejected.is_tier1_lan = False
    rejected.filter_reason = "lan_unconfirmed"
    alerts = []

    async def fake_get_new_finished_matches(**kwargs):
        kwargs["rejected_matches"].append(rejected)
        return []

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"limit": 30}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["tier1_lan_unconfirmed"] == 0
    assert alerts == []


def test_handler_uses_match_source_from_environment_default(monkeypatch):
    seen = {}

    async def fake_get_new_finished_matches(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(main, "MATCH_SOURCE", "liquipedia")
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"dry_run": True}, None)

    assert response["statusCode"] == 200
    assert seen["source"] == "liquipedia"


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


def test_telegram_uses_html_parse_mode(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(*args, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    main.send_to_telegram("chat", "<b>text</b>", max_attempts=1)

    assert seen["json"]["parse_mode"] == "HTML"
    assert seen["json"]["disable_web_page_preview"] is True


def test_telegram_photo_uses_media_spoiler_and_multipart(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    main.send_photo_to_telegram(
        "chat",
        b"png-data",
        "<b>result</b>",
        has_spoiler=True,
        max_attempts=1,
    )

    assert seen["url"].endswith("/sendPhoto")
    assert seen["data"]["parse_mode"] == "HTML"
    assert seen["data"]["has_spoiler"] == "true"
    assert seen["files"]["photo"][1] == b"png-data"
    assert seen["allow_redirects"] is False


def test_telegram_photo_rejects_caption_over_limit():
    with pytest.raises(main.TelegramDeliveryError, match="too long"):
        main.send_photo_to_telegram(
            "chat",
            b"png-data",
            "x" * (main.MAX_TELEGRAM_CAPTION_LENGTH + 1),
        )


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
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", None)
    monkeypatch.setattr(main, "LIQUIPEDIA_API_KEY", None)
    monkeypatch.setattr(main, "CHANNELS", [])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({}, None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {"error": "configuration_error"}
    assert called is False


def test_auto_accepts_liquipedia_as_only_configured_source(monkeypatch):
    called = False

    async def fake_get_new_finished_matches(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", None)
    monkeypatch.setattr(main, "LIQUIPEDIA_API_KEY", "liquipedia-key")
    monkeypatch.setattr(main, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({}, None)

    assert response["statusCode"] == 200
    assert called is True


def test_legacy_source_is_rejected_before_fetch(monkeypatch):
    called = False

    async def fake_get_new_finished_matches(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"source": "hltv", "dry_run": True}, None)

    assert response["statusCode"] == 400
    assert called is False


def test_schedule_is_formatted_in_moscow_time():
    local_now = main.datetime.fromisoformat("2026-07-30T10:00:00+03:00")

    text = main.format_daily_schedule([_upcoming()], local_now)

    assert "Матчи CS2 сегодня — 30 июля" in text
    assert "14:00 — <b>NAVI — FaZe</b>" in text
    assert "московск" not in text.casefold()


def test_schedule_photo_caption_omits_timezone_label():
    caption = main.format_schedule_photo_caption(
        main.datetime.fromisoformat("2026-07-30T10:00:00+03:00"),
        4,
    )

    assert "4 матча" in caption
    assert "московск" not in caption.casefold()


def test_digest_photo_caption_has_result_count():
    caption = main.format_digest_photo_caption(
        main.datetime.fromisoformat("2026-08-01T23:00:00+03:00"),
        2,
    )

    assert "Итоги дня — 1 августа" in caption
    assert "2 результата" in caption


def test_schedule_truncates_only_between_complete_entries():
    matches = []
    for index in range(100):
        match = _upcoming().model_copy(
            update={
                "match_id": str(index),
                "team1_name": f"Team {index} " + ("A" * 100),
                "team2_name": f"Opponent {index} " + ("B" * 100),
                "tournament_name": "IEM " + ("C" * 250),
            }
        )
        matches.append(match)

    text = main.format_daily_schedule(
        matches,
        main.datetime.fromisoformat("2026-07-30T10:00:00+03:00"),
    )

    assert len(text) <= main.MAX_TELEGRAM_MESSAGE_LENGTH
    assert "… и ещё " in text
    assert text.count("<b>") == text.count("</b>")


def test_schedule_dry_run_returns_preview_without_sending(monkeypatch):
    sent = []

    async def fake_fetch(start, end):
        assert start.isoformat() == "2026-07-29T21:00:00+00:00"
        assert end.isoformat() == "2026-07-30T21:00:00+00:00"
        return [_upcoming()]

    monkeypatch.setattr(
        main,
        "_local_day_window",
        lambda: (
            main.datetime.fromisoformat("2026-07-29T21:00:00+00:00"),
            main.datetime.fromisoformat("2026-07-30T21:00:00+00:00"),
            main.datetime.fromisoformat("2026-07-30T10:00:00+03:00"),
        ),
    )
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent.append(args))

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_selected"] == 1
    assert body["messages_sent"] == 1
    assert "14:00" in body["preview"]
    assert sent == []


def test_schedule_test_run_uses_separate_dedupe_key_and_label(monkeypatch):
    claimed = []
    sent_photos = []

    async def fake_fetch(start, end):
        return [_upcoming()]

    async def fake_claim(content_uid):
        claimed.append(content_uid)
        return "claim"

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(main, "render_schedule_card", lambda *args, **kwargs: b"card")
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: sent_photos.append((args, kwargs)),
    )

    response = main.handler(
        {"job": "schedule", "test_run_id": "media-card-20260731"},
        None,
    )
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 1
    assert body["test_run_id"] == "media-card-20260731"
    assert claimed[0].endswith("_test_media-card-20260731")
    assert sent_photos[0][0][1] == b"card"
    assert sent_photos[0][0][2].startswith("🧪 <b>Тестовая карточка</b>")


@pytest.mark.parametrize(
    "event",
    [
        {"job": "results", "test_run_id": "manual"},
        {"job": "schedule", "test_run_id": "../manual"},
        {"job": "schedule", "test_run_id": ""},
    ],
)
def test_invalid_test_run_id_is_rejected(event):
    response = main.handler(event, None)

    assert response["statusCode"] == 400


def test_digest_skips_publication_when_no_tier1_results(monkeypatch):
    async def fake_fetch(limit, start=None, end=None):
        match = _match()
        match.is_tier1_lan = False
        match.tournament_name = "Small Online Cup"
        return [match]

    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", fake_fetch)

    response = main.handler({"job": "digest", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 0
    assert body["matches_selected"] == 0


def test_digest_uses_spoiler_results_card_when_media_cards_enabled(monkeypatch):
    sent_photos = []
    sent_text = []

    async def fake_fetch(limit, start=None, end=None):
        return [_match("1"), _match("2", team1="Spirit", team2="MOUZ")]

    async def fake_claim(content_uid):
        return "claim"

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(main, "render_results_card", lambda *args, **kwargs: b"results-card")
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent_text.append(args))
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: sent_photos.append((args, kwargs)),
    )

    response = main.handler({"job": "digest"}, None)

    assert response["statusCode"] == 200
    assert sent_text == []
    assert sent_photos[0][0][1] == b"results-card"
    assert sent_photos[0][1]["has_spoiler"] is True
    assert sent_photos[0][1]["filename"].startswith("cs2-results-")


def test_invalid_job_is_rejected():
    response = main.handler({"job": "hourly", "dry_run": True}, None)

    assert response["statusCode"] == 400


def test_yandex_timer_payload_is_unwrapped(monkeypatch):
    async def fake_fetch(start, end):
        return [_upcoming()]

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    event = {
        "messages": [
            {
                "details": {
                    "payload": json.dumps({"job": "schedule", "dry_run": True})
                }
            }
        ]
    }

    response = main.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["job"] == "schedule"


def test_invalid_yandex_timer_payload_is_rejected():
    event = {"messages": [{"details": {"payload": "not-json"}}]}

    response = main.handler(event, None)

    assert response["statusCode"] == 400
