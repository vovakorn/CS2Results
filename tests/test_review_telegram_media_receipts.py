"""Telegram journal metadata describes the accepted format, including fallback."""
from unittest.mock import AsyncMock, Mock

import pytest

from cs2bot import main
from cs2bot.match_sources.models import MatchNormalized, TournamentPlacement, TournamentVRSImpact
from cs2bot.match_sources.storage import DeliveryClaim, PendingDelivery


@pytest.fixture(params=["tournament_standings", "tournament_vrs_standings"])
def telegram_receipt_runtime(request, monkeypatch):
    kind = request.param
    match = MatchNormalized(
        source="liquipedia", match_id="offline-final", tournament_name="Offline tournament",
        tournament_parent="Offline/Tournament", team1_name="A", team2_name="B",
        score1=2, score2=1, is_final=True, final_identity_confirmed=True,
        tournament_placements=[TournamentPlacement(placement=str(index + 1), team_name=name, prize_usd=1)
                               for index, name in enumerate(("A", "B"))],
    )
    impacts = tuple(TournamentVRSImpact(
        placement=str(index + 1), team_name=name, team_id=str(index),
        before_points=100, after_points=120, before_rank=3, after_rank=2,
        points_delta=20, rank_delta=1, source="Valve", before_version="before", after_version="after",
    ) for index, name in enumerate(("A", "B")))
    pending = PendingDelivery(
        key="outbox/offline", channel_id="global", channel_name="Global", match=match,
        created_at="2026-10-29T10:00:00Z", content_type=kind, generation=7, vrs_impacts=impacts,
    )
    claim = DeliveryClaim("offline-content", "claims/offline", "owner", "etag")
    for name, value in {
        "TELEGRAM_MEDIA_CARDS": True,
        "_delivery_is_current": Mock(return_value=True),
        "_vrs_tournament_id": Mock(return_value="offline-event"),
        "branding_for_match": Mock(return_value=None),
        "reconcile_content_delivery": AsyncMock(return_value=False),
        "claim_content_delivery": AsyncMock(return_value=claim),
        "mark_delivery_claim_attempting": AsyncMock(return_value=claim),
        "mark_delivery_claim_sent": AsyncMock(return_value=claim),
        "mark_delivery_claim_uncertain": AsyncMock(return_value=claim),
        "mark_content_processed": AsyncMock(),
        "delete_result_delivery": AsyncMock(),
        "release_delivery_claim": AsyncMock(),
        "record_result_delivery_attempt": AsyncMock(),
        "record_post": AsyncMock(),
        "record_publication": AsyncMock(),
        "send_photo_to_telegram": Mock(return_value={"ok": True}),
        "send_media_group_to_telegram": Mock(return_value={"ok": True}),
        "send_to_telegram": Mock(return_value={"ok": True}),
        "_notify_admin": Mock(),
        "log_event": Mock(),
    }.items():
        monkeypatch.setattr(main, name, value)
    renderer = Mock(return_value=[b"offline-png"])
    monkeypatch.setattr(main, "render_tournament_standings_cards", renderer)
    monkeypatch.setattr(main, "render_tournament_vrs_cards", renderer)
    deliver = main._deliver_tournament_standings if kind == "tournament_standings" else main._deliver_tournament_vrs
    return kind, pending, renderer, deliver


@pytest.mark.parametrize("card_count", [1, 2])
@pytest.mark.parametrize("outcome", ["media", "text_fallback", "media_uncertain", "text_uncertain"])
def test_telegram_media_receipt_tracks_accepted_photo_album_or_text(
    telegram_receipt_runtime, card_count, outcome,
):
    kind, pending, renderer, deliver = telegram_receipt_runtime
    renderer.return_value = [b"offline-png"] * card_count
    media_sender = main.send_photo_to_telegram if card_count == 1 else main.send_media_group_to_telegram
    if outcome in {"text_fallback", "text_uncertain"}:
        media_sender.side_effect = main.TelegramDeliveryError("definite media rejection")
    elif outcome == "media_uncertain":
        media_sender.side_effect = main.TelegramDeliveryUncertainError("unknown media outcome")
    if outcome == "text_uncertain":
        main.send_to_telegram.side_effect = main.TelegramDeliveryUncertainError("unknown text outcome")

    result = deliver(pending, {"chat_id": "offline-chat"}, "Global")
    media_sender.assert_called_once()
    if outcome.endswith("uncertain"):
        assert result == ("failed" if kind == "tournament_standings" else "uncertain")
        main.mark_delivery_claim_uncertain.assert_awaited_once()
        main.mark_delivery_claim_sent.assert_not_awaited()
        main.release_delivery_claim.assert_not_awaited()
        main.record_publication.assert_not_awaited()
        main.record_post.assert_not_awaited()
        if outcome == "media_uncertain":
            main.send_to_telegram.assert_not_called()
        else:
            main.send_to_telegram.assert_called_once()
        return

    assert result == "sent"
    main.mark_delivery_claim_sent.assert_awaited_once()
    main.mark_delivery_claim_uncertain.assert_not_awaited()
    main.release_delivery_claim.assert_not_awaited()
    main.record_post.assert_awaited_once()
    main.record_publication.assert_awaited_once()
    expected_media = outcome == "media"
    for record in (main.record_post, main.record_publication):
        assert record.call_args.kwargs["metadata"]["media_card"] is expected_media
        assert record.call_args.kwargs["metadata"]["generation"] == pending.generation
    if expected_media:
        main.send_to_telegram.assert_not_called()
    else:
        main.send_to_telegram.assert_called_once()
    if kind == "tournament_standings":
        succeeded = [call for call in main.log_event.call_args_list
                     if call.args[2] == "tournament_standings_delivery_succeeded"]
        assert succeeded[0].kwargs["media_card"] is expected_media


@pytest.mark.parametrize("media_mode", ["disabled", "render_failure", "empty_render"])
def test_telegram_media_receipt_is_text_when_no_cards_are_sent(
    telegram_receipt_runtime, monkeypatch, media_mode,
):
    _, pending, renderer, deliver = telegram_receipt_runtime
    if media_mode == "disabled":
        monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    elif media_mode == "render_failure":
        renderer.side_effect = RuntimeError("offline renderer failure")
    else:
        renderer.return_value = []
    assert deliver(pending, {"chat_id": "offline-chat"}, "Global") == "sent"
    main.send_to_telegram.assert_called_once()
    main.send_photo_to_telegram.assert_not_called()
    main.send_media_group_to_telegram.assert_not_called()
    main.record_publication.assert_awaited_once()
    assert main.record_publication.call_args.kwargs["metadata"]["media_card"] is False
