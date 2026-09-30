import json
from types import SimpleNamespace

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources.models import (
    MapResult,
    HeadToHead,
    MatchNormalized,
    RadarBracketMatch,
    ScheduleMatchContext,
    SourceReferences,
    TeamForm,
    TournamentPlacement,
    TournamentRadar,
    UpcomingMatchNormalized,
)
from cs2bot.match_sources.storage import DeliveryClaim, PendingDelivery, ThreadsChainAppend


async def _async(value):
    return value


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
    monkeypatch.setattr(main, "mark_delivery_claim_attempting", mark_sent)
    monkeypatch.setattr(main, "mark_delivery_claim_sent", mark_sent)
    monkeypatch.setattr(main, "mark_delivery_claim_uncertain", mark_sent)
    monkeypatch.setattr(main, "enqueue_result_delivery", enqueue_result)
    monkeypatch.setattr(main, "list_pending_result_deliveries", no_pending)
    monkeypatch.setattr(main, "record_result_delivery_attempt", no_op)
    monkeypatch.setattr(main, "delete_result_delivery", no_op)
    monkeypatch.setattr(main, "is_channel_processed", not_processed)
    monkeypatch.setattr(main, "is_telegram_media_degraded", not_processed)
    monkeypatch.setattr(main, "mark_telegram_media_degraded", no_op)
    monkeypatch.setattr(main, "clear_telegram_media_degraded", no_op)

    thread_tails = {}
    thread_reservations = set()
    async def reserve_thread(key):
        if key in thread_reservations:
            return None
        thread_reservations.add(key)
        return ThreadsChainAppend(key, f"append-{len(thread_reservations)}", thread_tails.get(key), '"etag"')
    async def confirm_thread(append, post_id):
        thread_tails[append.tournament_key] = post_id
        thread_reservations.discard(append.tournament_key)
    async def release_thread(append):
        thread_reservations.discard(append.tournament_key)
    async def block_thread(append):
        thread_reservations.add(append.tournament_key)
    monkeypatch.setattr(main, "reserve_threads_chain_append", reserve_thread)
    monkeypatch.setattr(main, "confirm_threads_chain_append", confirm_thread)
    monkeypatch.setattr(main, "release_threads_chain_append", release_thread)
    monkeypatch.setattr(main, "block_threads_chain_append", block_thread)


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


def test_format_match_adds_team_hashtags():
    text = main.format_match(_match(team1="Team Liquid", team2="Ninjas in Pyjamas"))

    assert text.endswith("#CS2 #РезультатыМатчей #TeamLiquid #NinjasinPyjamas")


def test_format_tournament_standings_lists_every_team_and_payout():
    text = main.format_tournament_standings(
        "IEM Cologne 2026",
        [
            TournamentPlacement(placement="1", team_name="NAVI", prize_usd=500_000),
            TournamentPlacement(placement="2", team_name="FaZe", prize_usd=180_000),
        ],
    )

    assert "<b>Итоги турнира — IEM Cologne 2026</b>" in text
    assert "1. NAVI — $500 000" in text
    assert "2. FaZe — $180 000" in text
    assert "#CS2 #ИтогиТурнира" in text


def test_format_tournament_standings_uses_the_supplied_source_label():
    text = main.format_tournament_standings(
        "BLAST Open Porto",
        [
            TournamentPlacement(placement="1", team_name="Spirit", prize_usd=150_000),
            TournamentPlacement(placement="2", team_name="MOUZ", prize_usd=60_000),
        ],
        source_label="BLAST.tv",
    )

    assert "Источник: BLAST.tv" in text


def test_tournament_standings_are_delivered_as_a_separate_confirmed_album(monkeypatch):
    match = _match().model_copy(
        update={
            "source": "liquipedia",
            "is_final": True,
            "tournament_parent": "BLAST/Open/Porto/2026",
            "tournament_placements": [
                TournamentPlacement(placement="1", team_name="NAVI", prize_usd=150_000),
                TournamentPlacement(placement="2", team_name="FaZe", prize_usd=60_000),
            ],
        }
    )
    pending = PendingDelivery(
        key="outbox/results/global_final-tournament_standings.json",
        channel_id="global",
        channel_name="Global",
        match=match,
        created_at="2026-09-06T10:00:00Z",
        content_type="tournament_standings",
    )
    claim = DeliveryClaim("content", "claims/content.json", "claim")
    sent = []
    marked = []
    deleted = []
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "claim_content_delivery", lambda *args: _async(claim))
    monkeypatch.setattr(main, "render_tournament_standings_cards", lambda *args: [b"card"])
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: (sent.append(args), {"ok": True})[1],
    )
    monkeypatch.setattr(main, "mark_content_processed", lambda uid, kind: _async(marked.append((uid, kind))))
    monkeypatch.setattr(main, "delete_result_delivery", lambda item: _async(deleted.append(item.key)))

    assert main._deliver_tournament_standings(pending, {"chat_id": "@global"}, "Global") == "sent"
    assert sent and sent[0][0] == "@global"
    assert marked == [("tournament-standings-v1:global:BLAST/Open/Porto/2026", "tournament_standings")]
    assert deleted == [pending.key]


@pytest.mark.parametrize(
    ("platform", "delivery", "publisher_name"),
    [
        ("instagram", main._deliver_instagram_tournament_standings, "publish_rendered_cards"),
        ("threads", main._deliver_threads_tournament_standings, "publish_threads_rendered_cards"),
    ],
)
def test_tournament_standings_are_delivered_to_each_social_platform(
    monkeypatch,
    platform,
    delivery,
    publisher_name,
):
    match = _match().model_copy(
        update={
            "source": "liquipedia",
            "is_final": True,
            "tournament_parent": "BLAST/Open/Porto/2026",
            "tournament_placements": [
                TournamentPlacement(placement="1", team_name="NAVI", prize_usd=150_000),
                TournamentPlacement(placement="2", team_name="FaZe", prize_usd=60_000),
            ],
        }
    )
    pending = PendingDelivery(
        key=f"outbox/results/{platform}_final-tournament_standings.json",
        channel_id=platform,
        channel_name=platform,
        match=match,
        created_at="2026-09-06T10:00:00Z",
        content_type="tournament_standings",
    )
    claim = DeliveryClaim("content", "claims/content.json", "claim")
    published = []
    marked = []
    monkeypatch.setattr(main, "claim_content_delivery", lambda *args: _async(claim))
    monkeypatch.setattr(main, "render_tournament_standings_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main, publisher_name, lambda *args: published.append(args))
    monkeypatch.setattr(main, "mark_content_processed", lambda uid, kind: _async(marked.append((uid, kind))))
    if platform == "threads":
        monkeypatch.setattr(
            main, "reserve_threads_chain_append",
            lambda key: _async(ThreadsChainAppend(key, "append", "previous-post", '"etag"')),
        )

    assert delivery(pending, None) == "sent"
    assert published and published[0][0] == f"{platform}_standings_BLAST_Open_Porto_2026"
    if platform == "threads":
        assert published[0][4] == "previous-post"
    assert marked == [(f"tournament-standings-v1:{platform}:BLAST/Open/Porto/2026", "tournament_standings")]


def test_threads_vrs_publication_replies_to_the_tournament_tail(monkeypatch):
    impact = main.TournamentVRSImpact(
        placement="1", team_name="NAVI", team_id="team-1",
        before_points=1800, after_points=1900, before_rank=2, after_rank=1,
        points_delta=100, rank_delta=1, source="Valve VRS",
        before_version="before", after_version="after",
    )
    second_impact = impact.model_copy(update={
        "placement": "2", "team_name": "FaZe", "team_id": "team-2",
        "before_points": 1700, "after_points": 1650,
        "before_rank": 4, "after_rank": 6, "points_delta": -50, "rank_delta": -2,
    })
    match = _match().model_copy(update={"vrs_baseline_id": "tournament-1"})
    pending = PendingDelivery(
        key="outbox/results/threads_vrs.json", channel_id="threads",
        channel_name="threads", match=match, created_at="2026-09-01T00:00:00Z",
        content_type="tournament_vrs_standings", vrs_impacts=(impact, second_impact),
    )
    published = []
    monkeypatch.setattr(main, "claim_content_delivery", lambda *args: _async(DeliveryClaim("vrs", "claim", "id")))
    monkeypatch.setattr(main, "render_tournament_vrs_cards", lambda *args: [b"vrs-card"])
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *args: (published.append(args), "vrs-post")[1])
    monkeypatch.setattr(
        main, "reserve_threads_chain_append",
        lambda key: _async(ThreadsChainAppend(key, "append", "standings-post", '"etag"')),
    )
    monkeypatch.setattr(main, "mark_content_processed", lambda *args: _async(None))

    assert main._deliver_threads_tournament_vrs(pending, None) == "sent"
    assert published[0][4] == "standings-post"


def test_instagram_content_delivery_has_separate_content_uid(monkeypatch):
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "reconcile_content_delivery", lambda *args, **kwargs: _async(False))
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async(_claim(_match(), uid)))
    monkeypatch.setattr(main, "mark_delivery_claim_sent", lambda claim: _async(claim))
    marked = []
    monkeypatch.setattr(main, "mark_content_processed", lambda uid, kind: _async(marked.append((uid, kind))))
    monkeypatch.setattr(main, "publish_rendered_cards", lambda *args: "media-id")

    sent, duplicates, failures = main._deliver_instagram_content(
        job="schedule",
        day_key="2026-08-30",
        cards=[b"card"],
        caption="<b>Schedule</b>",
        context=None,
        test_run_id=None,
    )

    assert (sent, duplicates, failures) == (1, 0, 0)
    assert marked == [("instagram_schedule_2026-08-30", "schedule")]


def test_instagram_content_delivery_alerts_on_safe_failure(monkeypatch):
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "reconcile_content_delivery", lambda *args, **kwargs: _async(False))
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async(_claim(_match(), uid)))
    monkeypatch.setattr(
        main,
        "publish_rendered_cards",
        lambda *args: (_ for _ in ()).throw(main.InstagramPublishError("HTTP 400")),
    )
    alerts = []
    monkeypatch.setattr(main, "_notify_admin", lambda code, message: alerts.append((code, message)))

    sent, duplicates, failures = main._deliver_instagram_content(
        job="digest",
        day_key="2026-08-30",
        cards=[b"card"],
        caption="digest",
        context=None,
        test_run_id=None,
    )

    assert (sent, duplicates, failures) == (0, 0, 1)
    assert alerts == [("instagram_content_publish_failed", "Instagram не опубликовал выпуск «digest»; потребуется повторная проверка.")]


def test_threads_content_delivery_has_separate_content_uid(monkeypatch):
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "reconcile_content_delivery", lambda *args, **kwargs: _async(False))
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async(_claim(_match(), uid)))
    monkeypatch.setattr(main, "mark_delivery_claim_sent", lambda claim: _async(claim))
    marked = []
    monkeypatch.setattr(main, "mark_content_processed", lambda uid, kind: _async(marked.append((uid, kind))))
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *args: "post-id")

    sent, duplicates, failures = main._deliver_threads_content(
        job="schedule",
        day_key="2026-08-30",
        cards=[b"card"],
        caption="caption",
        context=None,
        test_run_id=None,
    )

    assert (sent, duplicates, failures) == (1, 0, 0)
    assert marked == [("threads_schedule_2026-08-30", "schedule")]


@pytest.mark.parametrize(
    "delivery",
    [main._deliver_instagram_result, main._deliver_threads_result],
)
def test_social_duplicate_cleans_processed_outbox_item(monkeypatch, delivery):
    match = _match()
    pending = PendingDelivery(
        key=f"outbox/results/social_{match.match_uid}.json",
        channel_id="instagram" if delivery is main._deliver_instagram_result else "threads",
        channel_name="social",
        match=match,
        created_at="2026-08-30T10:00:00Z",
    )
    deleted = []
    monkeypatch.setattr(main, "claim_channel_delivery", lambda *args: _async(None))
    monkeypatch.setattr(main, "is_channel_processed", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(main, "delete_result_delivery", lambda item: _async(deleted.append(item.key)))

    assert delivery(pending, None) == "duplicate"
    assert deleted == [pending.key]


@pytest.mark.parametrize(
    ("delivery", "publisher_name", "error"),
    [
        (
            main._deliver_instagram_result,
            "publish_rendered_cards",
            main.InstagramDeliveryUncertainError("unknown"),
        ),
        (
            main._deliver_threads_result,
            "publish_threads_rendered_cards",
            main.ThreadsDeliveryUncertainError("unknown"),
        ),
    ],
)
def test_social_uncertain_result_is_removed_from_automatic_outbox(
    monkeypatch,
    delivery,
    publisher_name,
    error,
):
    match = _match()
    pending = PendingDelivery(
        key=f"outbox/results/social_{match.match_uid}.json",
        channel_id="social",
        channel_name="social",
        match=match,
        created_at="2026-08-30T10:00:00Z",
    )
    deleted = []
    monkeypatch.setattr(main, "claim_channel_delivery", lambda *args: _async(_claim(match, "social")))
    monkeypatch.setattr(main, "render_result_card", lambda value: b"card")
    monkeypatch.setattr(
        main,
        publisher_name,
        lambda *args: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(main, "delete_result_delivery", lambda item: _async(deleted.append(item.key)))
    monkeypatch.setattr(main, "_notify_admin", lambda *args: None)

    assert delivery(pending, None) == "uncertain"
    assert deleted == [pending.key]


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


def _x_pending(match):
    return PendingDelivery(
        key=f"outbox/results/x_{match.match_uid}.json",
        channel_id="x",
        channel_name="x",
        match=match,
        created_at="2026-02-17T00:00:00Z",
    )


def _enable_x_for_test(monkeypatch):
    monkeypatch.setenv("ENABLE_X_PUBLISHING", "1")
    monkeypatch.setattr(main, "_x_buffer_settings", lambda: ("test-key", "org-1", "buffer-channel"))

    async def no_x_checks(*args, **kwargs):
        return []

    monkeypatch.setattr(main.x_delivery, "check_due_x_deliveries", no_x_checks)


def test_x_result_handoff_uses_public_png_and_durable_delivery(monkeypatch):
    _enable_x_for_test(monkeypatch)
    match = _match()
    pending = _x_pending(match)
    upload_calls = []
    delivery_calls = []

    class FakeMediaClient:
        pass

    monkeypatch.setenv("X_MEDIA_BUCKET", "x-public")
    monkeypatch.setenv("X_MEDIA_PUBLIC_BASE_URL", "https://cdn.example")
    monkeypatch.setattr(main, "render_result_card", lambda _match: b"png-card")
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: FakeMediaClient())

    def upload(platform, publication_key, cards, **kwargs):
        upload_calls.append((platform, publication_key, cards, kwargs["bucket"], kwargs["base_url"]))
        return ["https://cdn.example/x/result_1/1.png"]

    async def create(uid, channel, organization, text, urls, api_key, **kwargs):
        delivery_calls.append((uid, channel, organization, text, urls, api_key))
        return SimpleNamespace(record=SimpleNamespace(status="accepted"))

    monkeypatch.setattr(main, "upload_public_pngs", upload)
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", create)

    outcome = main._deliver_x_result(pending)

    assert outcome == "accepted"
    assert upload_calls == [("x", f"result_{match.match_uid}", [b"png-card"], "x-public", "https://cdn.example")]
    uid, channel, organization, text, urls, api_key = delivery_calls[0]
    assert uid == f"x:result:{match.match_uid}"
    assert (channel, organization, api_key) == ("buffer-channel", "org-1", "test-key")
    assert urls == ["https://cdn.example/x/result_1/1.png"]
    assert "NAVI" in text and "FaZe" in text and "2026-02-17" in text


def test_enabled_x_result_is_queued_as_its_own_outbox_channel(monkeypatch):
    _enable_x_for_test(monkeypatch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    match = _match()
    enqueued = []
    deleted = []

    async def fake_matches(**kwargs):
        return [match]

    async def fake_enqueue(item, channel_id, channel_name):
        enqueued.append((channel_id, channel_name))
        return channel_id == "x"

    async def no_pending(*args, **kwargs):
        return []

    async def fake_delete(item):
        deleted.append(item.key)

    monkeypatch.setattr(main, "get_new_finished_matches", fake_matches)
    monkeypatch.setattr(main, "enqueue_result_delivery", fake_enqueue)
    monkeypatch.setattr(main, "list_pending_result_deliveries", no_pending)
    monkeypatch.setattr(main, "delete_result_delivery", fake_delete)
    monkeypatch.setattr(main, "_deliver_x_result", lambda _pending: "accepted")

    response = main.handler({}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert enqueued == [("global", "global"), ("x", "x")]
    assert body["messages_sent"] == 0
    assert body["per_channel"]["x"] == 0
    assert deleted == []


def test_x_flag_defaults_off_and_does_not_enqueue_or_check(monkeypatch):
    monkeypatch.delenv("ENABLE_X_PUBLISHING", raising=False)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    enqueued = []
    checked = []

    async def fake_matches(**kwargs):
        return [_match()]

    async def fake_enqueue(match, channel_id, channel_name):
        enqueued.append(channel_id)
        return False

    async def no_pending(*args, **kwargs):
        return []

    monkeypatch.setattr(main.x_delivery, "check_due_x_deliveries", lambda *args, **kwargs: checked.append(True))
    monkeypatch.setattr(main, "get_new_finished_matches", fake_matches)
    monkeypatch.setattr(main, "enqueue_result_delivery", fake_enqueue)
    monkeypatch.setattr(main, "list_pending_result_deliveries", no_pending)
    assert main.x_publishing_enabled() is False

    response = main.handler({}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert "x" not in body["per_channel"]
    assert enqueued == ["global"]
    assert checked == []


def test_x_accepted_outbox_item_stays_pending_across_retry_only_invocations(monkeypatch):
    _enable_x_for_test(monkeypatch)
    monkeypatch.setenv("X_MEDIA_BUCKET", "x-public")
    monkeypatch.setenv("X_MEDIA_PUBLIC_BASE_URL", "https://cdn.example")
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    pending = _x_pending(_match())
    attempts = []
    processed = []
    deleted = []
    create_post_calls = []
    state = {"status": None}

    monkeypatch.setattr(main, "render_result_card", lambda _match: b"png-card")
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        main,
        "upload_public_pngs",
        lambda *args, **kwargs: ["https://cdn.example/x/result/1.png"],
    )

    async def fake_pending(*args, **kwargs):
        return [pending]

    async def fake_processed(*args, **kwargs):
        return False

    async def fake_mark(*args, **kwargs):
        processed.append(args)

    async def fake_delete(item):
        deleted.append(item.key)

    async def fake_attempt(item):
        attempts.append(item.key)

    async def fake_create(uid, channel, organization, text, urls, api_key, **kwargs):
        if state["status"] is None:
            create_post_calls.append(True)
            state["status"] = "accepted"
        return SimpleNamespace(record=SimpleNamespace(status=state["status"]))

    monkeypatch.setattr(main, "list_pending_result_deliveries", fake_pending)
    monkeypatch.setattr(main, "is_channel_processed", fake_processed)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "delete_result_delivery", fake_delete)
    monkeypatch.setattr(main, "record_result_delivery_attempt", fake_attempt)
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)

    first = main.handler({"retry_only": True}, None)
    second = main.handler({"retry_only": True}, None)

    assert json.loads(first["body"])["messages_sent"] == 0
    assert json.loads(second["body"])["messages_sent"] == 0
    assert create_post_calls == [True]
    assert attempts == [pending.key, pending.key]
    assert processed == []
    assert deleted == []


def test_x_sent_confirmation_marks_processed_and_deletes_outbox(monkeypatch):
    _enable_x_for_test(monkeypatch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    pending = _x_pending(_match())
    processed = []
    deleted = []

    async def fake_pending(*args, **kwargs):
        return [pending]

    async def fake_processed(*args, **kwargs):
        return False

    async def fake_mark(*args, **kwargs):
        processed.append(args)

    async def fake_delete(item):
        deleted.append(item.key)

    monkeypatch.setattr(main, "list_pending_result_deliveries", fake_pending)
    monkeypatch.setattr(main, "is_channel_processed", fake_processed)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "delete_result_delivery", fake_delete)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_deliver_x_result", lambda _pending: "sent")

    response = main.handler({"retry_only": True}, None)

    body = json.loads(response["body"])
    assert body["messages_sent"] == 1
    assert body["per_channel"]["x"] == 1
    assert processed and processed[0][1] == "x"
    assert deleted == [pending.key]


def test_x_error_does_not_stop_other_result_channels(monkeypatch):
    _enable_x_for_test(monkeypatch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    match = _match()
    telegram = PendingDelivery(
        key=f"outbox/results/global_{match.match_uid}.json",
        channel_id="global", channel_name="global", match=match,
        created_at="2026-02-17T00:00:00Z",
    )
    x_item = _x_pending(match)
    instagram = PendingDelivery(
        key=f"outbox/results/instagram_{match.match_uid}.json",
        channel_id="instagram", channel_name="instagram", match=match,
        created_at="2026-02-17T00:00:00Z",
    )
    threads = PendingDelivery(
        key=f"outbox/results/threads_{match.match_uid}.json",
        channel_id="threads", channel_name="threads", match=match,
        created_at="2026-02-17T00:00:00Z",
    )
    sent = []

    async def fake_pending(*args, **kwargs):
        return [x_item, telegram, instagram, threads]

    async def fake_claim(*args, **kwargs):
        return _claim(match, "global")

    async def fake_mark(*args, **kwargs):
        return None

    def fail_x(_pending):
        raise RuntimeError("isolated X failure")

    monkeypatch.setattr(main, "list_pending_result_deliveries", fake_pending)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)
    monkeypatch.setattr(main, "_deliver_x_result", fail_x)
    monkeypatch.setattr(main, "_deliver_instagram_result", lambda *_args: "sent")
    monkeypatch.setattr(main, "_deliver_threads_result", lambda *_args: "sent")
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent.append(args) or {"ok": True})
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)

    response = main.handler({"retry_only": True}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 502
    assert len(sent) == 1
    assert body["messages_sent"] == 3
    assert body["delivery_failures"] == 1
    assert body["per_channel"]["instagram"] == 1
    assert body["per_channel"]["threads"] == 1


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
    delivery_events = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is False
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
        delivery_events.append("external_request")
        sent.append((chat_id, text))
        return {"ok": True}

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_mark(match, channel_name):
        delivery_events.append("processed")
        marked.append((match.match_uid, channel_name))

    async def fake_attempting(claim):
        delivery_events.append("attempting")
        return claim

    async def fake_sent(claim):
        delivery_events.append("sent")
        return claim

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_delivery_claim_attempting", fake_attempting)
    monkeypatch.setattr(main, "mark_delivery_claim_sent", fake_sent)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 1
    assert len(sent) == 1
    assert marked == [(_match().match_uid, "global")]
    assert delivery_events == ["attempting", "external_request", "sent", "processed"]


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
        raise main.TelegramDeliveryError("telegram failed")

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
    uncertain = []
    deleted = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
        raise main.TelegramDeliveryUncertainError("outcome is unknown")

    async def fake_claim(match, channel_id, legacy_channel_name=None):
        return _claim(match, channel_id)

    async def fake_release(claim):
        released.append(claim.match_uid)

    async def fake_uncertain(claim):
        uncertain.append(claim.match_uid)
        return claim

    async def fake_delete(pending):
        deleted.append(pending.key)

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "claim_channel_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "mark_delivery_claim_uncertain", fake_uncertain)
    monkeypatch.setattr(main, "delete_result_delivery", fake_delete)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 502
    assert released == []
    assert uncertain == [f"global_{_match().match_uid}"]
    assert len(deleted) == 1
    assert alerts[0][0] == "telegram_delivery_uncertain"


def test_schedule_keeps_claim_when_telegram_delivery_is_uncertain(monkeypatch):
    released = []
    alerts = []
    uncertain = []

    async def fake_fetch(start, end):
        return [_upcoming()]

    async def fake_claim(content_uid):
        return DeliveryClaim(content_uid, f"claims/{content_uid}.json", "claim", '"etag"')

    async def fake_release(claim):
        released.append(claim.match_uid)

    async def fake_uncertain(claim):
        uncertain.append(claim.match_uid)
        return claim

    def fake_send(*args, **kwargs):
        raise main.TelegramDeliveryUncertainError("outcome is unknown")

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "release_delivery_claim", fake_release)
    monkeypatch.setattr(main, "mark_delivery_claim_uncertain", fake_uncertain)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 502
    assert released == []
    assert len(uncertain) == 1
    assert alerts[0][0] == "telegram_delivery_uncertain"


def test_handler_marks_successful_channel_before_later_channel_fails(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7, max_attempts=3):
        sent.append(chat_id)
        if chat_id == "chat-b":
            raise main.TelegramDeliveryError("telegram failed")
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


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    ("job", "fetcher"),
    [
        ("results", "get_new_finished_matches"),
        ("schedule", "fetch_upcoming_matches"),
        ("digest", "fetch_pandascore_finished_matches"),
        ("radar_discovery", "fetch_upcoming_matches"),
    ],
)
def test_source_failure_only_alerts_outside_dry_run(monkeypatch, job, fetcher, dry_run):
    async def fail(*args, **kwargs):
        raise RuntimeError("source unavailable")

    alerts = []
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "test-chat", "teams": None}])
    monkeypatch.setattr(main, fetcher, fail)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: alerts.append(args))

    response = main.handler({"job": job, "dry_run": dry_run}, None)

    assert response["statusCode"] == 502
    assert len(alerts) == (0 if dry_run else 1)


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


def test_unknown_source_is_rejected_before_fetch(monkeypatch):
    called = False

    async def fake_get_new_finished_matches(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)

    response = main.handler({"source": "unsupported", "dry_run": True}, None)

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
    assert text.index("Турнир") < text.index("<b>NAVI — FaZe</b>")
    assert text.index("Турнир") < text.index("<b>Spirit — Vitality</b>")


def test_schedule_context_places_each_tournament_before_its_matches():
    first_match = _upcoming()
    second_match = _upcoming().model_copy(
        update={
            "match_id": "second-tournament-match",
            "tournament_name": "ESL Pro League",
            "competition_key": "ESL Pro League",
            "team1_name": "Spirit",
            "team2_name": "Vitality",
        }
    )
    contexts = {
        match.match_id: ScheduleMatchContext(
            match_id=match.match_id,
            team1_form=TeamForm(team_name=match.team1_name, wins=3, losses=2),
            team2_form=TeamForm(team_name=match.team2_name, wins=2, losses=3),
        )
        for match in (first_match, second_match)
    }

    text = main.format_schedule_context([first_match, second_match], contexts)

    first_match_start = text.index("<b>NAVI — FaZe</b>")
    second_header_start = text.index("🏆 Турнир ESL Pro League · Формат: Bo3")
    second_match_start = text.index("<b>Spirit — Vitality</b>")
    assert first_match_start < second_header_start < second_match_start


def test_schedule_context_splits_long_tournament_only_between_matches():
    matches = [
        _upcoming().model_copy(
            update={
                "match_id": f"long-context-{index}",
                "team1_name": f"Team {index} " + "A" * 160,
                "team2_name": f"Opponent {index} " + "B" * 160,
            }
        )
        for index in range(20)
    ]
    contexts = {
        match.match_id: ScheduleMatchContext(
            match_id=match.match_id,
            team1_form=TeamForm(team_name=match.team1_name, wins=3, losses=2),
            team2_form=TeamForm(team_name=match.team2_name, wins=2, losses=3),
            head_to_head=HeadToHead(match_count=0),
        )
        for match in matches
    }

    messages = main.format_schedule_context_messages(matches, contexts)
    combined = "\n".join(messages)

    assert len(messages) > 1
    assert all(len(message) <= main.MAX_TELEGRAM_MESSAGE_LENGTH for message in messages)
    assert all("Источник: PandaScore" in message for message in messages)
    assert combined.count("🏆 Турнир IEM Cologne 2026 · Формат: Bo3") == len(messages)
    for match in matches:
        assert combined.count(f"<b>{match.team1_name} — {match.team2_name}</b>") == 1


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


def test_tournament_radar_formats_confirmed_pairs_without_raw_ids():
    text = main.format_tournament_radar(
        TournamentRadar(
            tournament_id="3",
            bracket_matches=[
                RadarBracketMatch(match_id="1", team1_name="NAVI", team2_name="FaZe", round_name="Semifinal")
            ],
            roster_team_count=16,
            bracket_match_count=31,
        ),
        "IEM Cologne 2026",
    )

    assert "Турнирный радар — IEM Cologne 2026" in text
    assert "NAVI — FaZe" in text
    assert "Положение" not in text
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
            bracket_matches=[RadarBracketMatch(match_id="1", team1_name="NAVI", team2_name="FaZe")],
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
    assert body["radar"]["bracket_matches"][0]["team1_name"] == "NAVI"
    assert "IEM Cologne 2026" in body["preview"]


def _enable_x_radar_test(monkeypatch):
    monkeypatch.setenv("ENABLE_X_PUBLISHING", "1")
    monkeypatch.setenv("X_MEDIA_BUCKET", "x-public")
    monkeypatch.setenv("X_MEDIA_PUBLIC_BASE_URL", "https://cdn.example")
    monkeypatch.setattr(main, "_x_buffer_settings", lambda: ("test-key", "org-1", "buffer-channel"))
    monkeypatch.setattr(main, "get_x_delivery", lambda *args, **kwargs: _async(None))
    monkeypatch.setattr(main, "_capture_vrs_baseline", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    monkeypatch.setattr(main, "CHANNELS", [])
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)


@pytest.mark.parametrize("card_count", [1, 4, 6])
def test_x_radar_sends_only_complete_albums_up_to_four_cards(monkeypatch, card_count):
    _enable_x_radar_test(monkeypatch)
    radar = TournamentRadar(
        tournament_id="100",
        bracket_matches=[
            RadarBracketMatch(
                match_id=f"pair-{index}", team1_name=f"Team {index}", team2_name=f"Opponent {index}"
            )
            for index in range(1, 7)
        ],
    )
    cards = [f"card-{index}".encode() for index in range(1, card_count + 1)]
    rendered = []
    uploaded = []
    created = []

    def fake_render(rendered_radar, *args):
        rendered.append(rendered_radar)
        return cards

    def fake_upload(platform, publication_key, uploaded_cards, **kwargs):
        uploaded.append((platform, publication_key, list(uploaded_cards)))
        return [f"https://cdn.example/x/{publication_key}/{index}.png" for index in range(1, len(uploaded_cards) + 1)]

    async def fake_create(uid, channel, organization, text, urls, api_key):
        created.append((uid, list(urls)))
        return SimpleNamespace(record=SimpleNamespace(status="accepted"))

    monkeypatch.setattr(main, "render_tournament_radar_cards", fake_render)
    monkeypatch.setattr(main, "upload_public_pngs", fake_upload)
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)

    response = main._handle_radar_job(
        "100", "IEM Cologne 2026", False, publication_key="auto", radar=radar
    )
    body = json.loads(response["body"])

    assert rendered == [radar]
    assert body["x_cards_count"] == card_count
    if card_count <= 4:
        assert uploaded == [("x", "radar_100_auto", cards)]
        assert created == [("x:radar:100:auto", [f"https://cdn.example/x/radar_100_auto/{i}.png" for i in range(1, card_count + 1)])]
        assert body["x_delivery_state"] == "accepted"
        assert body["x_messages_sent"] == 0
    else:
        assert uploaded == []
        assert created == []
        assert body["x_skipped_reason"] == "album_over_four_images"
        assert body["x_delivery_state"] == "skipped"


def test_x_radar_dry_run_previews_without_buffer_or_upload(monkeypatch):
    _enable_x_radar_test(monkeypatch)
    radar = TournamentRadar(
        tournament_id="100",
        bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
    )
    calls = []
    monkeypatch.setattr(main, "render_tournament_radar_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main, "upload_public_pngs", lambda *args, **kwargs: calls.append("upload"))
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", lambda *args, **kwargs: calls.append("buffer"))

    response = main._handle_radar_job(
        "100", "IEM Cologne 2026", True, publication_key="auto", radar=radar
    )
    body = json.loads(response["body"])

    assert body["x_delivery_state"] == "dry_run"
    assert body["x_cards_count"] == 1
    assert "IEM Cologne 2026" in body["x_preview"]
    assert calls == []


def test_x_radar_repeat_uses_stable_record_without_second_create_or_upload(monkeypatch):
    _enable_x_radar_test(monkeypatch)
    radar = TournamentRadar(
        tournament_id="100",
        bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
    )
    record = {}
    uploads = []
    create_posts = []

    async def get_existing(uid, *, channel_id):
        current = record.get(uid)
        return SimpleNamespace(record=current) if current else None

    def upload(platform, key, cards, **kwargs):
        uploads.append(key)
        return [f"https://cdn.example/x/{key}/1.png"]

    async def create(uid, channel, organization, text, urls, api_key):
        if uid not in record:
            create_posts.append(uid)
            record[uid] = SimpleNamespace(status="accepted", text=text, image_urls=tuple(urls))
        return SimpleNamespace(record=record[uid])

    monkeypatch.setattr(main, "get_x_delivery", get_existing)
    monkeypatch.setattr(main, "render_tournament_radar_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main, "upload_public_pngs", upload)
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", create)

    first = main._handle_radar_job("100", "IEM Cologne 2026", False, publication_key="auto", radar=radar)
    second = main._handle_radar_job("100", "IEM Cologne 2026", False, publication_key="auto", radar=radar)

    assert json.loads(first["body"])["x_delivery_state"] == "accepted"
    assert json.loads(second["body"])["x_delivery_state"] == "accepted"
    assert create_posts == ["x:radar:100:auto"]
    assert uploads == ["radar_100_auto"]


def test_x_radar_failure_does_not_stop_telegram_or_threads(monkeypatch):
    _enable_x_radar_test(monkeypatch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: True)
    radar = TournamentRadar(
        tournament_id="100",
        bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
    )
    telegram = []
    threads = []

    async def fake_claim(uid):
        return DeliveryClaim(uid, f"claims/{uid}.json", "claim-id", '"etag"')

    async def no_op(*args, **kwargs):
        return None

    def fail_x(*args, **kwargs):
        raise RuntimeError("Buffer unavailable")

    monkeypatch.setattr(main, "render_tournament_radar_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", no_op)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: telegram.append(args))
    monkeypatch.setattr(main, "_deliver_threads_content", lambda **kwargs: threads.append(kwargs) or (1, 0, 0))
    monkeypatch.setattr(main, "upload_public_pngs", lambda platform, key, cards, **kwargs: [f"https://cdn.example/x/{key}/1.png"])
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fail_x)

    response = main._handle_radar_job("100", "IEM Cologne 2026", False, publication_key="auto", radar=radar)
    body = json.loads(response["body"])

    assert response["statusCode"] == 502
    assert len(telegram) == 1
    assert len(threads) == 1
    assert body["messages_sent"] == 1
    assert body["threads_messages_sent"] == 1
    assert body["x_delivery_failures"] == 1


def _x_tournament_pending(content_type="tournament_standings"):
    match = _match().model_copy(
        update={
            "source": "liquipedia",
            "is_final": True,
            "tournament_parent": "BLAST/Open/Porto/2026",
            "vrs_baseline_id": "BLAST/Open/Porto/2026",
            "tournament_placements": [
                TournamentPlacement(placement="1", team_name="NAVI", prize_usd=150_000),
                TournamentPlacement(placement="2", team_name="FaZe", prize_usd=60_000),
            ],
        }
    )
    impact = main.TournamentVRSImpact(
        placement="1",
        team_name="NAVI",
        team_id="team-1",
        before_points=1800,
        after_points=1900,
        before_rank=2,
        after_rank=1,
        points_delta=100,
        rank_delta=1,
        source="Valve VRS",
        before_version="standings_before.md",
        after_version="standings_after.md",
    )
    return PendingDelivery(
        key=f"outbox/results/x_{content_type}.json",
        channel_id="x",
        channel_name="x",
        match=match,
        created_at="2026-09-30T00:00:00Z",
        content_type=content_type,
        vrs_impacts=(impact,) if content_type == "tournament_vrs_standings" else (),
    )


def _enable_x_tournament_test(monkeypatch):
    monkeypatch.setenv("X_MEDIA_BUCKET", "x-public")
    monkeypatch.setenv("X_MEDIA_PUBLIC_BASE_URL", "https://cdn.example")
    monkeypatch.setattr(main, "_x_buffer_settings", lambda: ("test-key", "org-1", "buffer-channel"))

    async def no_existing(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "get_x_delivery", no_existing)
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    ("content_type", "render_name", "expected_uid", "expected_prefix"),
    [
        (
            "tournament_standings",
            "render_tournament_standings_cards",
            "x:tournament-standings:BLAST/Open/Porto/2026",
            "standings_BLAST_Open_Porto_2026",
        ),
        (
            "tournament_vrs_standings",
            "render_tournament_vrs_cards",
            "x:tournament-vrs:BLAST/Open/Porto/2026",
            "vrs_BLAST_Open_Porto_2026",
        ),
    ],
)
def test_x_tournament_formats_upload_complete_eligible_album(
    monkeypatch, content_type, render_name, expected_uid, expected_prefix
):
    _enable_x_tournament_test(monkeypatch)
    pending = _x_tournament_pending(content_type)
    rendered = []
    uploaded = []
    creates = []
    marked = []
    deleted = []
    attempts = []

    def fake_render(*args):
        rendered.append(args)
        return [b"card-1", b"card-2"]

    async def fake_create(uid, channel, organization, text, urls, api_key):
        creates.append((uid, channel, organization, text, list(urls), api_key))
        return SimpleNamespace(record=SimpleNamespace(status="accepted"))

    monkeypatch.setattr(main, render_name, fake_render)
    monkeypatch.setattr(
        main,
        "upload_public_pngs",
        lambda platform, key, cards, **kwargs: uploaded.append((platform, key, list(cards)))
        or [f"https://cdn.example/x/{key}/{index}.png" for index in range(1, len(cards) + 1)],
    )
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)
    monkeypatch.setattr(main, "mark_content_processed", lambda *args: _async(marked.append(args)))
    monkeypatch.setattr(main, "delete_result_delivery", lambda item: _async(deleted.append(item.key)))
    monkeypatch.setattr(main, "record_result_delivery_attempt", lambda item: _async(attempts.append(item.key)))

    outcome = main._process_x_tournament_outbox(pending)

    assert outcome == "accepted"
    assert len(rendered) == 1
    assert uploaded == [("x", expected_prefix, [b"card-1", b"card-2"])]
    assert creates[0][0] == expected_uid
    assert len(creates[0][4]) == 2
    assert marked == []
    assert deleted == []
    assert attempts == [pending.key]


@pytest.mark.parametrize(
    "source_case",
    ["unconfirmed-payout", "missing-vrs-impacts", "missing-snapshot-version"],
)
def test_x_tournament_formats_reject_incomplete_source_data(monkeypatch, source_case):
    _enable_x_tournament_test(monkeypatch)
    if source_case == "unconfirmed-payout":
        match = _match().model_copy(
            update={
                "source": "liquipedia",
                "is_final": True,
                "tournament_parent": "BLAST/Open/Porto/2026",
                "tournament_placements": [
                    TournamentPlacement(placement="1", team_name="NAVI", prize_usd=150_000),
                    TournamentPlacement(placement="2", team_name="FaZe", prize_usd=None),
                ],
            }
        )
        pending = PendingDelivery(
            key="outbox/results/x_unconfirmed_standings.json",
            channel_id="x",
            channel_name="x",
            match=match,
            created_at="2026-09-30T00:00:00Z",
            content_type="tournament_standings",
        )
    elif source_case == "missing-vrs-impacts":
        valid = _x_tournament_pending("tournament_vrs_standings")
        pending = PendingDelivery(
            key="outbox/results/x_bad_vrs.json",
            channel_id="x",
            channel_name="x",
            match=valid.match,
            created_at=valid.created_at,
            content_type="tournament_vrs_standings",
            vrs_impacts=(),
        )
    else:
        valid = _x_tournament_pending("tournament_vrs_standings")
        incomplete_impact = valid.vrs_impacts[0].model_copy(
            update={"before_version": "", "after_version": ""}
        )
        pending = PendingDelivery(
            key="outbox/results/x_missing_snapshot_version.json",
            channel_id="x",
            channel_name="x",
            match=valid.match,
            created_at=valid.created_at,
            content_type="tournament_vrs_standings",
            vrs_impacts=(incomplete_impact,),
        )
    calls = []
    monkeypatch.setattr(main, "render_tournament_standings_cards", lambda *args: calls.append("render"))
    monkeypatch.setattr(main, "render_tournament_vrs_cards", lambda *args: calls.append("render"))
    monkeypatch.setattr(main, "upload_public_pngs", lambda *args, **kwargs: calls.append("upload"))
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", lambda *args, **kwargs: calls.append("buffer"))
    deleted = []
    monkeypatch.setattr(main, "delete_result_delivery", lambda item: _async(deleted.append(item.key)))

    outcome = main._process_x_tournament_outbox(pending)

    assert outcome == "invalid_source"
    assert calls == []
    assert deleted == [pending.key]


@pytest.mark.parametrize("content_type", ["tournament_standings", "tournament_vrs_standings"])
def test_x_tournament_formats_defer_album_over_four_images(monkeypatch, content_type):
    _enable_x_tournament_test(monkeypatch)
    pending = _x_tournament_pending(content_type)
    calls = []
    render_name = (
        "render_tournament_standings_cards"
        if content_type == "tournament_standings"
        else "render_tournament_vrs_cards"
    )
    monkeypatch.setattr(main, render_name, lambda *args: [b"card"] * 5)
    monkeypatch.setattr(main, "upload_public_pngs", lambda *args, **kwargs: calls.append("upload"))
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", lambda *args, **kwargs: calls.append("buffer"))
    attempts = []
    monkeypatch.setattr(main, "record_result_delivery_attempt", lambda item: _async(attempts.append(item.key)))

    outcome = main._process_x_tournament_outbox(pending)

    assert outcome == "deferred_album_over_limit"
    assert calls == []
    assert attempts == [pending.key]


@pytest.mark.parametrize(
    ("content_type", "render_name", "expected_uid", "expected_prefix"),
    [
        (
            "tournament_standings",
            "render_tournament_standings_cards",
            "x:tournament-standings:BLAST/Open/Porto/2026",
            "standings_BLAST_Open_Porto_2026",
        ),
        (
            "tournament_vrs_standings",
            "render_tournament_vrs_cards",
            "x:tournament-vrs:BLAST/Open/Porto/2026",
            "vrs_BLAST_Open_Porto_2026",
        ),
    ],
)
def test_x_tournament_repeat_reuses_format_key_and_saved_payload(
    monkeypatch, content_type, render_name, expected_uid, expected_prefix
):
    _enable_x_tournament_test(monkeypatch)
    pending = _x_tournament_pending(content_type)
    stored = {}
    uploaded = []
    create_posts = []
    monkeypatch.setattr(main, render_name, lambda *args: [b"card"])

    async def get_existing(uid, *, channel_id):
        record = stored.get(uid)
        return SimpleNamespace(record=record) if record else None

    def fake_upload(platform, key, cards, **kwargs):
        uploaded.append(key)
        return [f"https://cdn.example/x/{key}/1.png"]

    async def fake_create(uid, channel, organization, text, urls, api_key):
        if uid not in stored:
            create_posts.append(uid)
            stored[uid] = SimpleNamespace(status="accepted", text=text, image_urls=tuple(urls))
        return SimpleNamespace(record=stored[uid])

    monkeypatch.setattr(main, "get_x_delivery", get_existing)
    monkeypatch.setattr(main, "upload_public_pngs", fake_upload)
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)
    monkeypatch.setattr(main, "record_result_delivery_attempt", lambda *args: _async(None))

    first = main._process_x_tournament_outbox(pending)
    second = main._process_x_tournament_outbox(pending)

    assert first == second == "accepted"
    assert create_posts == [expected_uid]
    assert uploaded == [expected_prefix]


def test_radar_discovery_selects_each_tier1_tournament_once():
    first = _upcoming().model_copy(
        update={
            "match_id": "first",
            "scheduled_at": "2026-08-31T09:00:00Z",
            "competition_key": "IEM Cologne 2026",
            "source_refs": SourceReferences(tournament_id="100"),
        }
    )
    later = first.model_copy(
        update={"match_id": "later", "scheduled_at": "2026-08-31T15:00:00Z"}
    )
    tier2 = first.model_copy(
        update={
            "match_id": "tier2",
            "source_refs": SourceReferences(tournament_id="200"),
            "feature_reason": "popular_team",
        }
    )

    candidates = main._radar_discovery_candidates([later, tier2, first])

    assert candidates == [("100", "IEM Cologne 2026", main.datetime.fromisoformat("2026-08-31T09:00:00+00:00"))]


def test_radar_discovery_keeps_full_tournament_name_instead_of_competition_key():
    match = _upcoming().model_copy(
        update={
            "tournament_name": "FISSURE PLAYGROUND — SEASON 3 2026 — GROUP A",
            "competition_key": "FISSURE Playground",
            "source_refs": SourceReferences(tournament_id="fissure-3-group-a"),
        }
    )

    candidates = main._radar_discovery_candidates([match])

    assert candidates[0][1] == "FISSURE PLAYGROUND — SEASON 3 2026 — GROUP A"


def test_radar_discovery_dry_run_previews_next_day_tournaments(monkeypatch):
    match = _upcoming().model_copy(
        update={
            "scheduled_at": "2026-08-31T09:00:00Z",
            "competition_key": "IEM Cologne 2026",
            "source_refs": SourceReferences(tournament_id="100"),
        }
    )

    async def fake_fetch(start, end):
        assert start < end
        return [match]

    async def fake_radar(tournament_id):
        assert tournament_id == "100"
        return TournamentRadar(
            tournament_id=tournament_id,
            earliest_match_at="2026-08-31T09:00:00Z",
            roster_team_count=16,
            bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
        )

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "fetch_tournament_radar", fake_radar)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(
        main,
        "_next_local_day_window",
        lambda: (
            main.datetime.fromisoformat("2026-08-31T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-08-30T12:00:00+03:00"),
        ),
    )

    response = main.handler({"job": "radar_discovery", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["tournaments_selected"] == 1
    assert body["messages_sent"] == 1
    assert body["radars"][0]["tournament_id"] == "100"
    assert "IEM Cologne 2026" in body["radars"][0]["preview"]


def test_radar_discovery_skips_tournament_without_confirmed_pairs(monkeypatch):
    match = _upcoming().model_copy(
        update={
            "scheduled_at": "2026-08-31T09:00:00Z",
            "competition_key": "IEM Cologne 2026",
            "source_refs": SourceReferences(tournament_id="100"),
        }
    )

    async def fake_fetch(*args):
        return [match]

    async def fake_radar(tournament_id):
        return TournamentRadar(
            tournament_id=tournament_id,
            earliest_match_at="2026-08-31T09:00:00Z",
            roster_team_count=16,
        )

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "fetch_tournament_radar", fake_radar)
    monkeypatch.setattr(
        main,
        "_next_local_day_window",
        lambda: (
            main.datetime.fromisoformat("2026-08-31T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-08-30T12:00:00+03:00"),
        ),
    )
    response = main.handler({"job": "radar_discovery", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 0
    assert body["radars"][0]["preview"] is None
    assert body["radars"][0]["skipped_reason"] == "bracket_unavailable"


def test_radar_discovery_skips_tournament_that_already_started(monkeypatch):
    match = _upcoming().model_copy(
        update={
            "scheduled_at": "2026-08-31T09:00:00Z",
            "source_refs": SourceReferences(tournament_id="100"),
        }
    )

    async def fake_fetch(*args):
        return [match]

    async def fake_radar(tournament_id):
        return TournamentRadar(
            tournament_id=tournament_id,
            earliest_match_at="2026-08-30T09:00:00Z",
            bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
        )

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "fetch_tournament_radar", fake_radar)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(
        main,
        "_next_local_day_window",
        lambda: (
            main.datetime.fromisoformat("2026-08-31T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-08-30T12:00:00+03:00"),
        ),
    )

    response = main.handler({"job": "radar_discovery", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 0
    assert body["skipped_reasons"] == {"tournament_already_started": 1}
    assert body["radars"][0]["skipped_reason"] == "tournament_already_started"


def test_radar_discovery_skips_tournament_with_unknown_start(monkeypatch):
    match = _upcoming().model_copy(
        update={
            "scheduled_at": "2026-08-31T09:00:00Z",
            "source_refs": SourceReferences(tournament_id="100"),
        }
    )

    async def fake_fetch(*args):
        return [match]

    async def fake_radar(tournament_id):
        return TournamentRadar(
            tournament_id=tournament_id,
            bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
        )

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "fetch_tournament_radar", fake_radar)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(
        main,
        "_next_local_day_window",
        lambda: (
            main.datetime.fromisoformat("2026-08-31T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
            main.datetime.fromisoformat("2026-08-30T12:00:00+03:00"),
        ),
    )

    response = main.handler({"job": "radar_discovery", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 0
    assert body["skipped_reasons"] == {"tournament_start_unknown": 1}
    assert body["radars"][0]["skipped_reason"] == "tournament_start_unknown"


def test_automatic_radar_uses_stable_tournament_deduplication_key(monkeypatch):
    claimed = []
    sent = []
    radar = TournamentRadar(
        tournament_id="100",
        earliest_match_at="2026-08-31T09:00:00Z",
        bracket_matches=[RadarBracketMatch(match_id="pair", team1_name="NAVI", team2_name="FaZe")],
    )

    async def fake_claim(content_uid):
        claimed.append(content_uid)
        if len(claimed) == 1:
            return DeliveryClaim("content_" + content_uid, "claim-key", "claim-id")
        return None

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent.append(args))

    first = main._handle_radar_job(
        "100", "IEM Cologne 2026", False, publication_key="auto", radar=radar
    )
    second = main._handle_radar_job(
        "100", "IEM Cologne 2026", False, publication_key="auto", radar=radar
    )

    assert json.loads(first["body"])["messages_sent"] == 1
    assert json.loads(second["body"])["duplicates_skipped"] == 1
    assert claimed == ["radar_100_auto_global", "radar_100_auto_global"]
    assert len(sent) == 1


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


def test_digest_photo_caption_adds_unique_team_hashtags():
    caption = main.format_digest_photo_caption(
        main.datetime.fromisoformat("2026-08-01T23:00:00+03:00"),
        2,
        ["Team Liquid", "NAVI", "Team Liquid"],
    )

    assert caption.endswith("#CS2 #ИтогиДня #TeamLiquid #NAVI")


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


def test_schedule_dry_run_keeps_tier1_and_excludes_qualifiers_and_lower_tiers(monkeypatch):
    tier1 = _upcoming().model_copy(
        update={"match_id": "tier1", "tournament_tier": "a"}
    )
    qualifier = _upcoming().model_copy(
        update={
            "match_id": "qualifier",
            "tournament_name": "IEM Beijing: Global Qualifier",
            "tournament_tier": "a",
            "team1_name": "3DMAX",
            "team2_name": "Heroic",
            "is_featured": False,
            "feature_reason": "excluded_tournament",
        }
    )
    lower_tier = _upcoming().model_copy(
        update={
            "match_id": "lower-tier",
            "tournament_name": "Regional League",
            "tournament_tier": "d",
            "team1_name": "Team One",
            "team2_name": "Team Two",
            "is_featured": False,
            "feature_reason": "not_featured",
        }
    )

    async def fake_fetch(*args):
        return [tier1, qualifier, lower_tier]

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_received"] == 3
    assert body["matches_selected"] == 1
    assert "NAVI vs FaZe" in body["preview"]
    assert "3DMAX vs Heroic" not in body["preview"]
    assert "Team One vs Team Two" not in body["preview"]


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

    async def no_context(matches):
        return []

    monkeypatch.setattr(main, "_local_day_window", fake_window)
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", no_context)
    monkeypatch.setattr(
        main,
        "_iter_channels",
        lambda: iter(({"id": "global", "name": "global", "chat_id": "@test", "teams": None},)),
    )

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
    assert body["matches_selected"] == 0
    assert body["messages_sent"] == 0
    assert body["days_ahead"] == 3
    assert body["window_start"] == "2026-07-29T21:00:00+00:00"
    assert body["window_end"] == "2026-08-01T21:00:00+00:00"
    assert body["preview"] is None
    assert body["diagnostics"][0]["teams"] == ["NAVI", "FaZe"]
    assert body["diagnostics"][0]["selected"] is False
    assert body["diagnostics"][0]["filter_reason"] == "excluded_tournament"


def test_schedule_dry_run_omits_filtered_diagnostics_by_default(monkeypatch):
    filtered = _upcoming().model_copy(
        update={"is_featured": False, "feature_reason": "not_featured"}
    )

    async def fake_fetch(start, end):
        return [filtered]

    async def no_context(matches):
        return []

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", no_context)

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert body["matches_received"] == 1
    assert body["matches_selected"] == 0
    assert body["diagnostics"] == []


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
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"schedule"])
    monkeypatch.setattr(main, "render_schedule_context_covers", lambda *args, **kwargs: [b"card"])
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
    assert sent_photos[0][0][1] == b"schedule"
    assert sent_photos[0][0][2].startswith("🧪 <b>Тестовая карточка</b>")
    assert sent_photos[1][0][1] == b"card"
    assert sent_photos[1][0][2].startswith("🧪 <b>Тестовая карточка</b>")


def test_schedule_attaches_single_context_note_to_its_cover(monkeypatch):
    photos = []
    sent_text = []

    async def fake_fetch(start, end):
        return [_upcoming()]

    async def fake_claim(content_uid):
        return "claim"

    async def fake_mark(*args, **kwargs):
        return None

    context_note = "🔎 <b>Контекст к матчам дня</b>\n\n<b>Очные встречи за 3 месяца:</b> команды не встречались."
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"schedule"])
    monkeypatch.setattr(main, "render_schedule_context_covers", lambda *args, **kwargs: [b"card"])
    monkeypatch.setattr(main, "format_schedule_context_messages", lambda *args, **kwargs: [context_note])
    monkeypatch.setattr(main, "send_photo_to_telegram", lambda *args, **kwargs: photos.append((args, kwargs)))
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: sent_text.append((args, kwargs)))

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 200
    assert [photo[0][1] for photo in photos] == [b"schedule", b"card"]
    assert "Матчи CS2 сегодня" in photos[0][0][2]
    assert "Контекст к матчам дня" not in photos[0][0][2]
    assert photos[1][0][2] == context_note
    assert sent_text == []


@pytest.mark.parametrize("context_failure", ["render", "rejected", "uncertain", "text"])
def test_context_failure_does_not_lose_or_repeat_confirmed_schedule(monkeypatch, context_failure):
    delivered = []
    processed = set()
    context_note = "🔎 <b>Контекст к матчам дня</b>"

    def render_context(*args):
        if context_failure == "render":
            raise RuntimeError("context cover unavailable")
        return [b"cover"]

    def send_photo(chat_id, photo, caption, **kwargs):
        delivered.append((photo, caption))
        if photo == b"cover":
            if context_failure == "uncertain":
                raise main.TelegramDeliveryUncertainError("unknown outcome")
            raise main.TelegramDeliveryError("rejected")

    def send_text(chat_id, text):
        delivered.append(("text", text))
        if context_failure == "text":
            raise main.TelegramDeliveryError("rejected")

    monkeypatch.setattr(main, "fetch_upcoming_matches", lambda *args: _async([_upcoming()]))
    monkeypatch.setattr(main, "_fetch_schedule_contexts", lambda matches: _async([None] * len(matches)))
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat"}])
    monkeypatch.setattr(main, "claim_content_delivery", lambda uid: _async("claim"))
    monkeypatch.setattr(main, "reconcile_content_delivery", lambda uid, job: _async(uid in processed))
    monkeypatch.setattr(main, "mark_content_processed", lambda uid, job: _async(processed.add(uid)))
    monkeypatch.setattr(main, "release_delivery_claim", lambda *args: pytest.fail("Confirmed schedule must stay claimed"))
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args: [b"schedule"])
    monkeypatch.setattr(main, "render_schedule_context_covers", render_context)
    monkeypatch.setattr(main, "format_schedule_context_messages", lambda *args: [context_note])
    monkeypatch.setattr(main, "send_photo_to_telegram", send_photo)
    monkeypatch.setattr(main, "send_to_telegram", send_text)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["messages_sent"] == 1
    assert delivered[0][0] == b"schedule"
    assert len(processed) == 1
    if context_failure == "uncertain":
        assert len(delivered) == 2  # No text fallback after an ambiguous cover send.
    else:
        assert delivered[-1] == ("text", context_note)
    before_retry = list(delivered)
    assert main.handler({"job": "schedule"}, None)["statusCode"] == 200
    assert delivered == before_retry


@pytest.mark.parametrize("cover_count", [1, 2])
@pytest.mark.parametrize("messages", [["context " * 200], ["first context", "second context"]])
def test_long_or_multiple_context_notes_remain_complete(monkeypatch, cover_count, messages):
    delivered = []
    covers = [b"cover"] * cover_count
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "render_schedule_context_covers", lambda *args: covers)
    monkeypatch.setattr(main, "send_photo_to_telegram", lambda chat, photo, caption, **kwargs: delivered.append(("photo", caption)))
    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda chat, photos, caption, **kwargs: delivered.append(("album", caption)))
    monkeypatch.setattr(main, "send_to_telegram", lambda chat, text: delivered.append(("text", text)))

    main._send_schedule_context_to_telegram(
        {"name": "global", "chat_id": "chat"},
        messages,
        [_upcoming()],
        main.datetime.fromisoformat("2026-09-06T10:00:00+03:00"),
        None,
    )

    assert delivered[0] == ("photo" if cover_count == 1 else "album", "🔎 <b>Контекст к матчам дня</b>")
    assert delivered[1:] == [("text", message) for message in messages]


def test_schedule_keeps_telegram_and_social_card_policies_independent(monkeypatch):
    telegram_text = []
    instagram_cards = []

    async def fake_fetch(start, end):
        return [_upcoming()]

    async def fake_claim(content_uid):
        return "claim"

    async def fake_mark(*args, **kwargs):
        return None

    def fake_instagram_delivery(**kwargs):
        instagram_cards.extend(kwargs["cards"])
        return 1, 0, 0

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark)
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"social-card"])
    monkeypatch.setattr(
        main,
        "render_schedule_context_covers",
        lambda *args, **kwargs: pytest.fail("Telegram covers must stay disabled"),
    )
    monkeypatch.setattr(main, "send_to_telegram", lambda chat_id, text: telegram_text.append(text))
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: pytest.fail("Telegram must remain text-only"),
    )
    monkeypatch.setattr(main, "_deliver_instagram_content", fake_instagram_delivery)

    response = main.handler({"job": "schedule"}, None)

    assert response["statusCode"] == 200
    assert telegram_text
    assert instagram_cards == [b"social-card"]


def test_busy_schedule_album_precedes_context_cover(monkeypatch):
    claimed = []
    photos = []
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
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"page-1", b"page-2"])
    monkeypatch.setattr(
        main,
        "render_schedule_context_covers",
        lambda *args, **kwargs: [b"cover"],
    )
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: photos.append((args, kwargs)),
    )
    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: albums.append((args, kwargs)))
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: None)

    response = main.handler({"job": "schedule"}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["matches_selected"] == 16
    assert body["messages_sent"] == 1
    assert albums[0][0][1] == [b"page-1", b"page-2"]
    assert "РасписаниеМатчей" in albums[0][0][2]
    assert photos[0][0][1] == b"cover"
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
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args, **kwargs: [b"page-1", b"page-2"])
    monkeypatch.setattr(
        main,
        "render_schedule_context_covers",
        lambda *args, **kwargs: [b"cover"],
    )
    monkeypatch.setattr(
        main,
        "send_photo_to_telegram",
        lambda *args, **kwargs: (_ for _ in ()).throw(main.TelegramDeliveryError("failed")),
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
    assert "Матчи CS2 сегодня" in sent_text[0][1]
    assert "14:00" in sent_text[0][1]
    assert "Контекст к матчам дня" in sent_text[1][1]


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
    monkeypatch.setattr(main, "render_results_cards", lambda *args, **kwargs: [b"results-card"])
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


def test_digest_sends_every_result_page_in_one_spoiler_album(monkeypatch):
    albums = []

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
    monkeypatch.setattr(main, "render_results_cards", lambda *args, **kwargs: [b"page-1", b"page-2"])
    monkeypatch.setattr(main, "send_media_group_to_telegram", lambda *args, **kwargs: albums.append((args, kwargs)))
    monkeypatch.setattr(main, "send_photo_to_telegram", lambda *args, **kwargs: pytest.fail("lost album pages"))

    response = main.handler({"job": "digest"}, None)

    assert response["statusCode"] == 200
    assert len(albums) == 1
    assert albums[0][0][1] == [b"page-1", b"page-2"]
    assert albums[0][1]["has_spoiler"] is True
    assert all(name.startswith("cs2-results-") for name in albums[0][1]["filenames"])


def _enable_x_content_job(monkeypatch):
    monkeypatch.setenv("ENABLE_X_PUBLISHING", "1")
    monkeypatch.setenv("X_MEDIA_BUCKET", "x-public")
    monkeypatch.setenv("X_MEDIA_PUBLIC_BASE_URL", "https://cdn.example")
    monkeypatch.setattr(main, "_x_buffer_settings", lambda: ("test-key", "org-1", "buffer-channel"))
    monkeypatch.setattr(
        main,
        "CHANNELS",
        [{"id": "global", "name": "global", "chat_id": "test-chat", "teams": None}],
    )
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)

    async def no_telegram_claim(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "claim_content_delivery", no_telegram_claim)

    async def no_existing_x_delivery(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "get_x_delivery", no_existing_x_delivery)


@pytest.mark.parametrize("job", ["schedule", "digest"])
def test_x_content_jobs_handoff_complete_release_to_buffer(monkeypatch, job):
    _enable_x_content_job(monkeypatch)
    uploaded = []
    handed_off = []

    async def fake_schedule(start, end):
        return [_upcoming()]

    async def fake_contexts(matches):
        return []

    async def fake_digest(limit, start=None, end=None):
        return [_match()]

    async def fake_create(uid, channel, organization, text, urls, api_key):
        handed_off.append((uid, channel, organization, text, urls, api_key))
        return SimpleNamespace(record=SimpleNamespace(status="accepted"))

    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_schedule)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", fake_contexts)
    monkeypatch.setattr(main, "fetch_pandascore_finished_matches", fake_digest)
    monkeypatch.setattr(main, "apply_quality_filters", lambda matches: (matches, [], []))
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args: [b"schedule-card"])
    monkeypatch.setattr(main, "render_results_cards", lambda *args: [b"digest-card"])
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        main,
        "upload_public_pngs",
        lambda platform, key, cards, **kwargs: uploaded.append((platform, key, cards))
        or [f"https://cdn.example/x/{key}/1.png"],
    )
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)

    response = main.handler({"job": job}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert len(uploaded) == 1
    assert uploaded[0][0] == "x"
    assert uploaded[0][1].startswith(f"{job}_")
    assert uploaded[0][2] == [b"schedule-card" if job == "schedule" else b"digest-card"]
    uid, channel, organization, post_text, urls, api_key = handed_off[0]
    assert uid.startswith(f"x:{job}:")
    assert (channel, organization, api_key) == ("buffer-channel", "org-1", "test-key")
    assert urls[0].startswith("https://cdn.example/x/")
    assert post_text and body["x_cards_count"] == 1
    assert body["x_delivery_state"] == "accepted"
    assert body["x_messages_sent"] == 0


def test_x_content_empty_release_has_explicit_skip_reason(monkeypatch):
    _enable_x_content_job(monkeypatch)
    uploads = []
    async def fake_fetch(start, end):
        return []
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "upload_public_pngs", lambda *args, **kwargs: uploads.append(True))

    response = main.handler({"job": "schedule"}, None)
    body = json.loads(response["body"])

    assert body["matches_selected"] == 0
    assert body["x_cards_count"] == 0
    assert body["x_skipped_reason"] == "empty_issue"
    assert uploads == []


def test_x_content_skips_album_with_more_than_four_cards_before_upload():
    matches = [
        SimpleNamespace(
            competition_key=f"event-{index}",
            tournament_name=f"Event {index}",
            scheduled_at=f"2026-09-30T{10 + index:02d}:00:00Z",
        )
        for index in range(5)
    ]

    result = main._deliver_x_content_job(
        "schedule", "2026-09-30", matches, None, None  # type: ignore[arg-type]
    )

    assert result["x_cards_count"] == 5
    assert result["x_skipped_reason"] == "invalid_card_count"
    assert result["x_delivery_state"] == "skipped"


def test_x_content_dry_run_previews_without_render_upload_or_buffer(monkeypatch):
    _enable_x_content_job(monkeypatch)
    calls = []
    async def fake_fetch(start, end):
        return [_upcoming()]
    async def fake_contexts(matches):
        return []
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", fake_contexts)
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args: calls.append("render"))
    monkeypatch.setattr(main, "upload_public_pngs", lambda *args, **kwargs: calls.append("upload"))
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", lambda *args, **kwargs: calls.append("buffer"))

    response = main.handler({"job": "schedule", "dry_run": True}, None)
    body = json.loads(response["body"])

    assert body["x_delivery_state"] == "dry_run"
    assert body["x_cards_count"] == 1
    assert "NAVI" in body["x_preview"] and "FaZe" in body["x_preview"]
    assert calls == []


def test_x_content_repeated_scheduler_invocation_keeps_one_create_post(monkeypatch):
    _enable_x_content_job(monkeypatch)
    async def fake_fetch(start, end):
        return [_upcoming()]
    async def fake_contexts(matches):
        return []
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", fake_contexts)
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    uploads = []
    monkeypatch.setattr(main, "upload_public_pngs", lambda platform, key, cards, **kwargs: uploads.append(key) or [f"https://cdn.example/x/{key}/1.png"])
    persisted = {}
    create_post_calls = []

    async def fake_get(uid, *, channel_id):
        record = persisted.get(uid)
        if record is None:
            return None
        return SimpleNamespace(record=record)

    monkeypatch.setattr(main, "get_x_delivery", fake_get)

    async def fake_create(uid, channel, organization, text, urls, api_key):
        if uid not in persisted:
            create_post_calls.append(uid)
            persisted[uid] = SimpleNamespace(
                status="accepted", text=text, image_urls=tuple(urls)
            )
        return SimpleNamespace(record=persisted[uid])

    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)
    first = main.handler({"job": "schedule"}, None)
    second = main.handler({"job": "schedule"}, None)

    assert json.loads(first["body"])["x_delivery_state"] == "accepted"
    assert json.loads(second["body"])["x_delivery_state"] == "accepted"
    assert len(create_post_calls) == 1
    assert len(uploads) == 1
    assert create_post_calls[0].startswith("x:schedule:")


def test_x_buffer_error_does_not_stop_telegram_content_delivery(monkeypatch):
    _enable_x_content_job(monkeypatch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "chat", "teams": None}])
    async def fake_fetch(start, end):
        return [_upcoming()]
    async def fake_contexts(matches):
        return []
    async def fake_claim(uid):
        return "telegram-claim"
    async def fake_mark_processed(*args, **kwargs):
        return None
    telegram_sent = []
    async def fake_create(*args, **kwargs):
        raise RuntimeError("Buffer unavailable")
    monkeypatch.setattr(main, "fetch_upcoming_matches", fake_fetch)
    monkeypatch.setattr(main, "_fetch_schedule_contexts", fake_contexts)
    monkeypatch.setattr(main, "claim_content_delivery", fake_claim)
    monkeypatch.setattr(main, "mark_content_processed", fake_mark_processed)
    monkeypatch.setattr(main, "send_to_telegram", lambda *args, **kwargs: telegram_sent.append(args))
    monkeypatch.setattr(main, "render_schedule_cards", lambda *args: [b"card"])
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "upload_public_pngs", lambda platform, key, cards, **kwargs: [f"https://cdn.example/x/{key}/1.png"])
    monkeypatch.setattr(main.x_delivery, "create_x_delivery", fake_create)

    response = main.handler({"job": "schedule"}, None)
    body = json.loads(response["body"])

    assert len(telegram_sent) >= 1
    assert telegram_sent[0][0] == "chat"
    assert body["x_delivery_failures"] == 1
    assert body["x_delivery_state"] == "error"
    assert body["messages_sent"] == 1


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


def test_threads_chain_publishes_root_then_reply_and_keeps_tournaments_independent(monkeypatch):
    tails = {}
    active = set()
    calls = []

    async def reserve(key):
        if key in active:
            return None
        active.add(key)
        return ThreadsChainAppend(key, f"a-{len(calls)}", tails.get(key), '"etag"')

    async def confirm(append, post_id):
        tails[append.tournament_key] = post_id
        active.remove(append.tournament_key)

    async def clear(append):
        active.discard(append.tournament_key)

    monkeypatch.setattr(main, "reserve_threads_chain_append", reserve)
    monkeypatch.setattr(main, "confirm_threads_chain_append", confirm)
    monkeypatch.setattr(main, "release_threads_chain_append", clear)
    monkeypatch.setattr(main, "block_threads_chain_append", clear)

    def publisher(key, cards, caption, context, reply_to_id=None):
        calls.append((key, reply_to_id))
        return f"post-{len(calls)}"

    monkeypatch.setattr(main, "publish_threads_rendered_cards", publisher)
    main._publish_threads_chain("threads:pandascore:one", "card-1", [b"1"], "one", None)
    main._publish_threads_chain("threads:pandascore:two", "card-2", [b"2"], "two", None)
    main._publish_threads_chain("threads:pandascore:one", "card-3", [b"3"], "one", None)
    assert calls == [("card-1", None), ("card-2", None), ("card-3", "post-1")]


def test_threads_chain_uncertain_append_blocks_only_its_tournament(monkeypatch):
    active = set()

    async def reserve(key):
        if key in active:
            return None
        active.add(key)
        return ThreadsChainAppend(key, key, None, '"etag"')

    async def block(append):
        active.add(append.tournament_key)

    async def clear(append):
        active.discard(append.tournament_key)

    monkeypatch.setattr(main, "reserve_threads_chain_append", reserve)
    monkeypatch.setattr(main, "block_threads_chain_append", block)
    monkeypatch.setattr(main, "release_threads_chain_append", clear)
    monkeypatch.setattr(main, "publish_threads_rendered_cards", lambda *args: (_ for _ in ()).throw(main.ThreadsDeliveryUncertainError("unknown")))

    with pytest.raises(main.ThreadsDeliveryUncertainError):
        main._publish_threads_chain("threads:pandascore:one", "one", [b"1"], "one", None)
    assert "threads:pandascore:one" in active
    assert "threads:pandascore:two" not in active


def test_threads_schedule_groups_cards_by_stable_tournament_id():
    first = _upcoming().model_copy(update={
        "match_id": "match-1", "source_refs": SourceReferences(tournament_id="tournament-1"),
    })
    second = _upcoming().model_copy(update={
        "match_id": "match-2", "source_refs": SourceReferences(tournament_id="tournament-1"),
    })
    other = _upcoming().model_copy(update={
        "match_id": "match-3", "tournament_name": "ESL Pro League",
        "competition_key": "ESL Pro League",
        "source_refs": SourceReferences(tournament_id="tournament-2"),
    })

    groups = main._group_threads_content("schedule", [first, second, other], "2026-09-21")

    assert list(groups) == ["threads:pandascore:tournament-1", "threads:pandascore:tournament-2"]
    assert [item.match_id for item in groups["threads:pandascore:tournament-1"]] == ["match-1", "match-2"]
    assert [item.match_id for item in groups["threads:pandascore:tournament-2"]] == ["match-3"]
