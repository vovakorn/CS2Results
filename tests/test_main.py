import json

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources.models import (
    MapResult,
    HeadToHead,
    MatchNormalized,
    ScheduleMatchContext,
    TeamForm,
    TournamentRadar,
    UpcomingMatchNormalized,
)
from cs2bot.match_sources.storage import DeliveryClaim, PendingDelivery


@pytest.fixture(autouse=True)
def configured_runtime(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(main, "OBJECT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", "pandascore-token")

    async def no_reconciliation(*args, **kwargs):
        return False

    async def mark_sent(claim, *args, **kwargs):
        return claim

    async def enqueue_result(*args, **kwargs):
        return True

    async def no_pending(*args, **kwargs):
        return []

    async def no_op(*args, **kwargs):
        return None

    async def not_processed(*args, **kwargs):
        return False

    monkeypatch.setattr(main, "reconcile_channel_delivery", no_reconciliation)
    monkeypatch.setattr(main, "reconcile_content_delivery", no_reconciliation)
    monkeypatch.setattr(main, "mark_delivery_claim_sent", mark_sent)
    monkeypatch.setattr(main, "enqueue_result_delivery", enqueue_result)
    monkeypatch.setattr(main, "list_pending_result_deliveries", no_pending)
    monkeypatch.setattr(main, "record_result_delivery_attempt", no_op)
    monkeypatch.setattr(main, "delete_result_delivery", no_op)
    monkeypatch.setattr(main, "is_channel_processed", not_processed)
    monkeypatch.setattr(main, "is_telegram_media_degraded", not_processed)
    monkeypatch.setattr(main, "mark_telegram_media_degraded", no_op)
    monkeypatch.setattr(main, "clear_telegram_media_degraded", no_op)


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
    assert "Победитель:" not in text
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


def test_format_match_truncation_preserves_html_structure():
    match = _match(team1="&" * 200, team2="&" * 200)
    match.tournament_name = "IEM " + ("&" * 200)
    match.maps = [MapResult(name="&" * 100, score1=13, score2=11) for _ in range(10)]
    match.match_url = "https://liquipedia.net/counterstrike/Match"
    match.source = "liquipedia"

    text = main.format_match(match)

    assert len(text) <= main.MAX_TELEGRAM_MESSAGE_LENGTH
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<a ") == text.count("</a>")
    assert "…" in text

    truncated_link = main._truncate_telegram_html(
        '<a href="https://liquipedia.net/counterstrike/Match">' + ("x" * 5000) + "</a>",
        100,
    )
    assert truncated_link.endswith("</a>")
    assert truncated_link.count("<a ") == truncated_link.count("</a>")


def test_result_delivery_uses_recovery_timeout_and_retry_budget():
    assert main.RESULT_TELEGRAM_TIMEOUT_SECONDS == 10
    assert main.RESULT_TELEGRAM_MAX_ATTEMPTS == 2
    assert main.RESULT_TEXT_TELEGRAM_MAX_ATTEMPTS == 1


def test_handler_dry_run_does_not_send_or_mark(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is True
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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
                "tournament_tier_type": None,
                "publisher_tier": None,
                "tournament_section": None,
                "teams": ["NAVI", "FaZe"],
            "score": [2, 1],
            "date": "2026-02-17",
            "start_date": None,
            "end_date": None,
            "original_scheduled_at": None,
                "rescheduled": None,
                "forfeit": None,
                "result_type": None,
                "team_result_statuses": [None, None],
                "date_exact": None,
                "vod_url": None,
                "is_lan": None,
            "location": None,
            "is_tier1_lan": True,
            "filter_reason": None,
            "tier1_autopilot_selected": False,
            "tier1_autopilot_reason": "tier_unknown",
        }
    ]
    assert sent == []
    assert marked == []


def test_analytics_import_does_not_require_telegram_or_match_source(monkeypatch):
    recorded = []

    async def fake_record(channel_id, message_id, views, reactions):
        recorded.append((channel_id, message_id, views, reactions))

    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", "")
    monkeypatch.setattr(main, "CHANNELS", [])
    monkeypatch.setattr(main, "record_manual_post_metrics", fake_record)

    response = main.handler(
        {
            "job": "analytics",
            "analytics_operation": "import_metrics",
            "channel_id": "global",
            "message_id": 42,
            "views_24h": 100,
        },
        None,
    )

    assert response["statusCode"] == 200
    assert recorded == [("global", 42, 100, None)]


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
    assert sent_photos[0][1]["timeout"] == main.RESULT_TELEGRAM_TIMEOUT_SECONDS
    assert sent_photos[0][1]["max_attempts"] == main.RESULT_TELEGRAM_MAX_ATTEMPTS
    assert "<tg-spoiler>2 : 1</tg-spoiler>" in sent_photos[0][0][2]


def test_result_skips_media_after_soft_budget_and_sends_bounded_text(monkeypatch):
    sent_text = []
    match = _match()
    monotonic_values = iter([0.0, 1.0, main.RESULT_MEDIA_BUDGET_SECONDS + 1.0])

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
    monkeypatch.setattr(main, "_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        main,
        "render_result_card",
        lambda item: pytest.fail("card rendering must be skipped after the soft budget"),
    )
    monkeypatch.setattr(
        main,
        "send_to_telegram",
        lambda *args, **kwargs: sent_text.append((args, kwargs)),
    )

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 200
    assert sent_text[0][1]["timeout"] == main.RESULT_TELEGRAM_TIMEOUT_SECONDS
    assert sent_text[0][1]["max_attempts"] == main.RESULT_TEXT_TELEGRAM_MAX_ATTEMPTS


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


def test_result_connect_timeout_enables_text_only_mode(monkeypatch):
    sent_text = []
    degraded_until = []
    match = _match()

    async def fake_get_new_finished_matches(**kwargs):
        return [match]

    async def fake_claim(*args, **kwargs):
        return _claim(match, "global")

    async def fake_mark_degraded(value):
        degraded_until.append(value)

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "mark_telegram_media_degraded", fake_mark_degraded)
    monkeypatch.setattr(main, "render_result_card", lambda item: b"card")
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            main.TelegramConnectTimeoutError("connect failed")
        ),
    )
    monkeypatch.setattr(
        main,
        "send_to_telegram",
        lambda *args, **kwargs: sent_text.append(args) or {"ok": True},
    )

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 200
    assert len(sent_text) == 1
    assert len(degraded_until) == 1


def test_result_delivers_durable_outbox_item_after_source_drops_it(monkeypatch):
    sent = []
    deleted = []
    match = _match()
    pending = PendingDelivery(
        key=f"outbox/results/global_{match.match_uid}.json",
        channel_id="global",
        channel_name="global",
        match=match,
        created_at="2026-08-28T00:00:00Z",
        last_attempt_at="2026-08-28T00:05:00Z",
        attempt_count=1,
    )

    async def fake_get_new_finished_matches(**kwargs):
        pytest.fail("retry-only invocation must not call PandaScore")

    async def fake_pending(*args, **kwargs):
        return [pending]

    async def fake_claim(*args, **kwargs):
        return _claim(match, "global")

    async def fake_delete(item):
        deleted.append(item.key)

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "PANDASCORE_API_TOKEN", None)
    monkeypatch.setattr(main, "LIQUIPEDIA_API_KEY", None)
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "list_pending_result_deliveries", fake_pending)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "delete_result_delivery", fake_delete)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(
        main,
        "send_to_telegram",
        lambda *args, **kwargs: sent.append(args) or {"ok": True},
    )

    response = main.handler({"retry_only": True}, None)

    assert response["statusCode"] == 200
    assert len(sent) == 1
    assert deleted == [pending.key]
    assert json.loads(response["body"])["retry_only"] is True


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


def test_handler_dry_run_reports_liquipedia_shadow_aggregates(monkeypatch):
    async def fake_get_new_finished_matches(**kwargs):
        kwargs["shadow_diagnostics"].update(
            {
                "matched": 8,
                "primary_only": 2,
                "liquipedia_only": 1,
                "score_mismatches": 0,
            }
        )
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"dry_run": True}, None)
    body = json.loads(response["body"])

    assert body["liquipedia_shadow"] == {
        "matched": 8,
        "primary_only": 2,
        "liquipedia_only": 1,
        "score_mismatches": 0,
    }


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

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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


def test_handler_keeps_claim_when_telegram_delivery_is_uncertain(monkeypatch):
    released = []
    alerts = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
        raise main.TelegramDeliveryUncertainError("outcome is unknown")

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_release(claim):
        released.append(claim.match_uid)

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 502
    assert released == []
    assert alerts[0][0] == "telegram_delivery_uncertain"


def test_schedule_keeps_claim_when_telegram_delivery_is_uncertain(monkeypatch):
    released = []
    alerts = []

    async def fake_fetch(start, end):
        return [_upcoming()]

    async def fake_claim(content_uid):
        return DeliveryClaim(content_uid, f"claims/{content_uid}.json", "claim", '"etag"')

    async def fake_release(claim):
        released.append(claim.match_uid)

    def fake_send(*args, **kwargs):
        raise main.TelegramDeliveryUncertainError("outcome is unknown")

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 502
    assert released == []
    assert alerts[0][0] == "telegram_delivery_uncertain"


def test_handler_marks_successful_channel_before_later_channel_fails(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
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


def test_telegram_media_group_uses_multipart_attachments(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": []}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    main.send_media_group_to_telegram(
        "chat",
        [b"page-1", b"page-2"],
        "<b>16 матчей</b>",
        filenames=["schedule-1.png", "schedule-2.png"],
        max_attempts=1,
    )

    media = json.loads(seen["data"]["media"])
    assert seen["url"].endswith("/sendMediaGroup")
    assert media[0]["media"] == "attach://photo0"
    assert media[0]["caption"] == "<b>16 матчей</b>"
    assert "caption" not in media[1]
    assert seen["files"]["photo0"] == ("schedule-1.png", b"page-1", "image/png")
    assert seen["files"]["photo1"] == ("schedule-2.png", b"page-2", "image/png")
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


def test_telegram_connect_timeout_is_retried_and_safe_to_release(monkeypatch):
    calls = 0

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectTimeout("connection timed out")
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)

    main.send_to_telegram("chat", "text", max_attempts=2)

    assert calls == 2


def test_telegram_connect_timeout_is_not_delivery_uncertain(monkeypatch):
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectTimeout()))

    with pytest.raises(main.TelegramDeliveryError) as exc_info:
        main.send_to_telegram("chat", "text", max_attempts=1)

    assert not isinstance(exc_info.value, main.TelegramDeliveryUncertainError)


def test_member_count_network_exception_never_exposes_token(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:SECRET")

    def fail(*args, **kwargs):
        raise requests.ConnectTimeout(
            "failed for https://api.telegram.org/bot123456:SECRET/getChatMemberCount"
        )

    monkeypatch.setattr(main.requests, "post", fail)

    with pytest.raises(main.TelegramDeliveryUncertainError) as exc_info:
        main.get_telegram_member_count("chat")

    assert "SECRET" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_telegram_proxy_is_used_for_delivery(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(*args, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(main, "TELEGRAM_PROXY_URL", "https://proxy.example")
    monkeypatch.setattr(main.requests, "post", fake_post)

    main.send_to_telegram("chat", "text", max_attempts=1)

    assert seen["proxies"] == {
        "http": "https://proxy.example",
        "https": "https://proxy.example",
    }


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
    assert "🏆 <b>IEM Cologne 2026</b>" in text
    assert "14:00 — <b>NAVI vs FaZe</b>" in text
    assert "🕙" not in text
    assert "московск" not in text.casefold()


def test_daily_schedule_groups_four_matches_under_one_tournament():
    matches = [
        _upcoming().model_copy(
            update={
                "match_id": str(index),
                "team1_name": team1,
                "team2_name": team2,
                "scheduled_at": scheduled_at,
                "tournament_name": "Esports World Cup — 2026 — Playoffs",
            }
        )
        for index, (scheduled_at, team1, team2) in enumerate(
            [
                ("2026-08-19T17:00:00+03:00", "FUT Esports", "magic"),
                ("2026-08-19T20:00:00+03:00", "GamerLegion", "MOUZ"),
                ("2026-08-19T17:00:00+03:00", "G2", "Astralis"),
                ("2026-08-19T20:00:00+03:00", "FURIA", "Aurora Gaming"),
            ]
        )
    ]

    text = main.format_daily_schedule(matches, main.datetime.fromisoformat("2026-08-19T10:00:00+03:00"))

    assert text.count("🏆 <b>Esports World Cup 2026 · Playoffs</b>") == 1
    assert text.index("17:00 — <b>FUT Esports vs magic</b>") < text.index("20:00 — <b>GamerLegion vs MOUZ</b>")
    assert "🏆 Другие матчи" not in text


def test_daily_schedule_separates_two_tournaments_and_handles_missing_name():
    matches = []
    for index in range(10):
        matches.append(
            _upcoming().model_copy(
                update={
                    "match_id": str(index),
                    "team1_name": f"Team {index}",
                    "team2_name": f"Opponent {index}",
                    "scheduled_at": f"2026-08-19T{12 + index:02d}:00:00+03:00",
                    "tournament_name": "IEM Cologne 2026" if index < 5 else "BLAST Premier — 2026 — Finals",
                }
            )
        )
    matches.append(
        _upcoming().model_copy(
            update={
                "match_id": "missing-tournament",
                "team1_name": "Unknown A",
                "team2_name": "Unknown B",
                "scheduled_at": "2026-08-19T22:00:00+03:00",
                "tournament_name": "",
            }
        )
    )

    text = main.format_daily_schedule(matches, main.datetime.fromisoformat("2026-08-19T10:00:00+03:00"))

    assert text.count("🏆 <b>IEM Cologne 2026</b>") == 1
    assert text.count("🏆 <b>BLAST Premier 2026 · Finals</b>") == 1
    assert "🏆 <b>Другие матчи</b>" in text
    assert "</b>\n\n🏆 <b>" in text


def test_schedule_context_explains_form_and_series_format():
    match = _upcoming()
    context = ScheduleMatchContext(
        match_id=match.match_id,
        tournament_id="3",
        team1_form=TeamForm(team_name="NAVI", wins=4, losses=1),
        team2_form=TeamForm(team_name="FaZe", wins=2, losses=3),
        head_to_head=HeadToHead(match_count=3, team1_wins=2, team2_wins=1),
        team1_roster_size=5,
        team2_roster_size=5,
    )

    text = main.format_schedule_context([match], {match.match_id: context})

    assert "Контекст к матчам дня" in text
    assert "<b>Последние 5 матчей каждой команды:</b> NAVI — 4 победы и 1 поражение; FaZe — 2 победы и 3 поражения." in text
    assert "не рейтинг команд и не прогноз" in text
    assert "<b>Очные встречи за 3 месяца:</b> 3 матча. <b>NAVI</b>  2 : 1  <b>FAZE</b>." in text
    assert "🏆 Турнир IEM Cologne 2026 · Формат: Bo3" in text
    assert text.index("🏆 Турнир IEM Cologne 2026") < text.index("NAVI — FaZe")
    assert text.index("не рейтинг команд и не прогноз") > text.index("<b>Последние 5 матчей каждой команды:</b>")


def test_schedule_context_shows_shared_format_once_before_matches():
    first_match = _upcoming()
    second_match = _upcoming().model_copy(
        update={"match_id": "second-match", "team1_name": "Spirit", "team2_name": "Vitality"}
    )
    first_context = ScheduleMatchContext(
        match_id=first_match.match_id,
        tournament_id="3",
        team1_form=TeamForm(team_name="NAVI", wins=4, losses=1),
        team2_form=TeamForm(team_name="FaZe", wins=2, losses=3),
    )
    second_context = ScheduleMatchContext(
        match_id=second_match.match_id,
        tournament_id="3",
        team1_form=TeamForm(team_name="Spirit", wins=3, losses=2),
        team2_form=TeamForm(team_name="Vitality", wins=2, losses=3),
    )

    text = main.format_schedule_context(
        [first_match, second_match],
        {first_match.match_id: first_context, second_match.match_id: second_context},
    )

    assert text.count("🏆 Турнир IEM Cologne 2026 · Формат: Bo3") == 1
    assert text.index("🏆 Турнир") < text.index("<b>NAVI — FaZe</b>")
    assert text.index("🏆 Турнир") < text.index("<b>Spirit — Vitality</b>")


def test_schedule_context_groups_tournament_stages_and_omits_match_format():
    group_a = _upcoming().model_copy(
        update={
            "tournament_name": "BLAST Open — Porto — Group A",
            "competition_key": "BLAST Open — Porto",
        }
    )
    group_b = _upcoming().model_copy(
        update={
            "match_id": "group-b",
            "tournament_name": "BLAST Open — Porto — Group B",
            "competition_key": "BLAST Open — Porto",
            "team1_name": "G2",
            "team2_name": "Natus Vincere",
        }
    )
    contexts = {
        match.match_id: ScheduleMatchContext(
            match_id=match.match_id,
            team1_form=TeamForm(team_name=match.team1_name, wins=3, losses=2),
            team2_form=TeamForm(team_name=match.team2_name, wins=2, losses=3),
            head_to_head=HeadToHead(match_count=0),
        )
        for match in (group_a, group_b)
    }

    text = main.format_schedule_context([group_a, group_b], contexts)

    assert text.count("🏆 Турнир BLAST Open — Porto · Формат: Bo3") == 1
    assert "<b>G2 — Natus Vincere</b> · Bo3" not in text
    assert "<b>G2 — Natus Vincere</b>" in text
    assert text.count("<b>Очные встречи за 3 месяца:</b> команды не встречались.") == 2


def test_schedule_context_keeps_match_when_its_context_request_fails():
    match = _upcoming()

    text = main.format_schedule_context([match], {})

    assert "<b>NAVI — FaZe</b>" in text
    assert "Контекст по командам пока недоступен." in text


def test_schedule_context_does_not_turn_recent_results_into_a_prediction():
    match = _upcoming().model_copy(update={"best_of": 1})
    context = ScheduleMatchContext(
        match_id=match.match_id,
        tournament_id="3",
        team1_form=TeamForm(team_name="NAVI", wins=4, losses=1),
        team2_form=TeamForm(team_name="FaZe", wins=4, losses=1),
    )

    text = main.format_schedule_context([match], {match.match_id: context})

    assert "явного фаворита" not in text
    assert "не рейтинг команд и не прогноз" in text
    assert "🏆 Турнир IEM Cologne 2026 · Формат: Bo1" in text


def test_tournament_radar_formats_standings_without_raw_ids():
    text = main.format_tournament_radar(
        TournamentRadar(
            tournament_id="3",
            standings=["1. NAVI", "2. FaZe"],
            roster_team_count=16,
            bracket_match_count=31,
        ),
        "IEM Cologne 2026",
    )

    assert "Турнирный радар — IEM Cologne 2026" in text
    assert "1. NAVI" in text
    assert "Участников: 16" in text
    assert "tournament_id" not in text


def test_schedule_dry_run_includes_optional_context(monkeypatch):
    async def fake_fetch(*args):
        return [_upcoming()]

    async def fake_context(match):
        return ScheduleMatchContext(
            match_id=match.match_id,
            tournament_id="3",
            team1_form=TeamForm(team_name="NAVI", wins=3, losses=2),
            team2_form=TeamForm(team_name="FaZe", wins=4, losses=1),
        )

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "fetch_schedule_match_context", fake_context)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["context_matches_ready"] == 1
    assert "<b>Последние 5 матчей каждой команды:</b> NAVI — 3 победы и 2 поражения; FaZe — 4 победы и 1 поражение." in body["context_preview"]


def test_radar_dry_run_returns_preview_without_sending(monkeypatch):
    async def fake_radar(tournament_id):
        assert tournament_id == "3"
        return TournamentRadar(
            tournament_id=tournament_id,
            standings=["1. NAVI"],
            roster_team_count=8,
            bracket_match_count=15,
        )

    monkeypatch.setattr(main, "fetch_tournament_radar", fake_radar)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])

    response = main.handler(
        {
            "job": "radar",
            "tournament_id": 3,
            "tournament_name": "IEM Cologne 2026",
            "dry_run": True,
        },
        None,
    )
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 1
    assert body["radar"]["standings"] == ["1. NAVI"]
    assert "IEM Cologne 2026" in body["preview"]


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
        lambda **kwargs: (
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


def test_schedule_dry_run_reports_filtered_matches_across_requested_window(monkeypatch):
    filtered = _upcoming().model_copy(
        update={
            "match_id": "filtered-1",
            "tournament_name": "Regional Open Qualifier",
            "is_featured": False,
            "feature_reason": "excluded_tournament",
        }
    )

    def fake_window(*, days_ahead=1):
        assert days_ahead == 3
        return (
            main.datetime.fromisoformat("2026-07-29T21:00:00+00:00"),
            main.datetime.fromisoformat("2026-08-01T21:00:00+00:00"),
            main.datetime.fromisoformat("2026-07-30T10:00:00+03:00"),
        )

    async def fake_fetch(start, end):
        assert start.isoformat() == "2026-07-29T21:00:00+00:00"
        assert end.isoformat() == "2026-08-01T21:00:00+00:00"
        return [filtered]

    monkeypatch.setattr(main, "_local_day_window", fake_window)
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)

    async def no_context(matches):
        return []

    monkeypatch.setattr(main, "_fetch_schedule_contexts", no_context)

    response = main.handler(
        {
            "job": "schedule",
            "dry_run": True,
            "include_filtered": True,
            "days_ahead": 3,
        },
        None,
    )
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_received"] == 1
    assert body["matches_selected"] == 1
    assert body["messages_sent"] == 0
    assert body["days_ahead"] == 3
    assert body["window_start"] == "2026-07-29T21:00:00+00:00"
    assert body["window_end"] == "2026-08-01T21:00:00+00:00"
    assert "NAVI vs FaZe" in body["preview"]
    assert body["diagnostics"][0]["teams"] == ["NAVI", "FaZe"]
    assert body["diagnostics"][0]["selected"] is False
    assert body["diagnostics"][0]["filter_reason"] == "excluded_tournament"


def test_schedule_dry_run_omits_filtered_diagnostics_by_default(monkeypatch):
    filtered = _upcoming().model_copy(
        update={"is_featured": False, "feature_reason": "not_featured"}
    )

    async def fake_fetch(start, end):
        return [filtered]

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)

    async def no_context(matches):
        return []

    monkeypatch.setattr(main, "_fetch_schedule_contexts", no_context)

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert body["matches_received"] == 1
    assert body["matches_selected"] == 1
    assert body["diagnostics"][0]["selected"] is False


@pytest.mark.parametrize("days_ahead", [0, 8, True, "3"])
def test_invalid_schedule_days_ahead_is_rejected(days_ahead):
    response = main.handler(
        {"job": "schedule", "dry_run": True, "days_ahead": days_ahead},
        None,
    )

    assert response["statusCode"] == 400


def test_multi_day_schedule_is_blocked_outside_dry_run():
    response = main.handler({"job": "schedule", "days_ahead": 3}, None)

    assert response["statusCode"] == 400


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
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"card"])
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


def test_busy_schedule_is_sent_as_one_two_card_album(monkeypatch):
    claimed = []
    albums = []
    marked = []
    matches = [
        _upcoming().model_copy(update={"match_id": f"match-{index}"})
        for index in range(16)
    ]

    async def fake_fetch(start, end):
        return matches

    async def fake_claim(content_uid):
        claimed.append(content_uid)
        return "claim"

    async def fake_mark(content_uid, job):
        marked.append((content_uid, job))

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat"}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(
        main,
        "render_schedule_cards",
        lambda *args, **kwargs: [b"page-1", b"page-2"],
    )
    monkeypatch.setattr(
        main,
        "send_media_group_to_telegram",
        lambda *args, **kwargs: albums.append((args, kwargs)),
    )
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: pytest.fail("busy schedule must use an album"),
    )

    response = main.handler({"job": "schedule"}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_selected"] == 16
    assert body["messages_sent"] == 1
    assert albums[0][0][1] == [b"page-1", b"page-2"]
    assert albums[0][0][2] == main.format_schedule_photo_caption(
        main._local_day_window()[2], 16
    )
    assert albums[0][1]["filenames"][0].endswith("-1-of-2.png")
    assert albums[0][1]["filenames"][1].endswith("-2-of-2.png")
    assert marked == [(claimed[0], "schedule")]


def test_busy_schedule_album_failure_falls_back_to_text(monkeypatch):
    sent_text = []
    matches = [
        _upcoming().model_copy(update={"match_id": f"match-{index}"})
        for index in range(16)
    ]

    async def fake_fetch(start, end):
        return matches

    async def fake_claim(content_uid):
        return "claim"

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat"}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(
        main,
        "render_schedule_cards",
        lambda *args, **kwargs: [b"page-1", b"page-2"],
    )
    monkeypatch.setattr(
        main,
        "send_media_group_to_telegram",
        lambda *args, **kwargs: (_ for _ in ()).throw(main.TelegramDeliveryError("failed")),
    )
    monkeypatch.setattr(
        main,
        "send_to_telegram",
        lambda chat_id, text: sent_text.append((chat_id, text)),
    )

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 200
    assert sent_text and sent_text[0][0] == "chat"


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
