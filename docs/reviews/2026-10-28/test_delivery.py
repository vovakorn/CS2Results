"""Offline audit proofs for b363ac00 / production source 54c912c.

These tests deliberately assert the observed defects, rather than the desired
post-fix behavior. Every external operation is replaced with an in-memory mock.
Run from the audited snapshot with its absolute path in PYTHONPATH.
"""
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests

from cs2bot import instagram_publish, main, threads_publish
from cs2bot.match_sources.models import TournamentPlacement


@pytest.mark.parametrize("destination,publication_type", [
    ("global", "schedule"), ("instagram", "schedule"), ("threads", "schedule"),
    ("global", "schedule_context"),
    ("global", "radar"), ("instagram", "radar"), ("threads", "radar"),
])
def test_review_reproduces_fresh_formats_blocked_after_reenable(destination, publication_type):
    # update_policy increases generation on disable and retains it on enable.
    with patch.object(main, "publication_delivery_allowed", return_value=(True, 1)):
        if publication_type in {"schedule", "radar"}:
            assert main._daily_delivery_is_current(destination, publication_type) is False
        else:
            assert main._delivery_is_current(destination, publication_type) is False
        # A freshly captured generation, as used by digest, succeeds.
        assert main._daily_delivery_is_current(destination, "digest", {destination: 1}) is True


@pytest.mark.parametrize("module,credentials_name,endpoint,uncertain", [
    (instagram_publish, "_instagram_credentials", "media", instagram_publish.InstagramDeliveryUncertainError),
    (threads_publish, "_threads_credentials", "threads", threads_publish.ThreadsDeliveryUncertainError),
])
def test_review_reproduces_container_connect_timeout_treated_as_uncertain_publish(
    module, credentials_name, endpoint, uncertain,
):
    requests_made = []

    def no_connection(url, **kwargs):
        requests_made.append(url.rsplit("/", 1)[-1])
        raise requests.ConnectTimeout("offline audit: connection never established")

    with (
        patch.object(module, credentials_name, return_value=("offline-dummy", "user")),
        patch.object(module, "_meta_proxy", return_value=nullcontext(None)),
        patch.object(module.requests, "post", side_effect=no_connection),
    ):
        with pytest.raises(uncertain):
            module.publish_cards(["https://example.invalid/card.png"], "Offline audit", None)
    # No endpoint that can publish a post has been called.
    assert requests_made == [endpoint]


@pytest.mark.parametrize("platform", ["instagram", "threads"])
def test_review_reproduces_result_loss_after_container_failure(platform):
    # The primitive proof above shows this exception can arise before publish.
    # Verify how each caller interprets that exception and handles its outbox.
    error_type = (instagram_publish.InstagramDeliveryUncertainError if platform == "instagram"
                  else threads_publish.ThreadsDeliveryUncertainError)
    publisher = "publish_rendered_cards" if platform == "instagram" else "publish_threads_rendered_cards"
    pending = SimpleNamespace(
        match=SimpleNamespace(match_uid="audit-match", source="pandascore"),
        channel_id=platform, generation=0,
    )
    replacements = {
        "_delivery_is_current": Mock(return_value=True),
        "reconcile_channel_delivery": AsyncMock(return_value=False),
        "claim_channel_delivery": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "render_result_card": Mock(return_value=b"offline-png"),
        "format_match": Mock(return_value="Offline match"),
        "mark_delivery_claim_attempting": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_delivery_claim_uncertain": AsyncMock(),
        "delete_result_delivery": AsyncMock(),
        "release_delivery_claim": AsyncMock(),
        "_notify_admin": Mock(),
        "_threads_tournament_key": Mock(return_value="threads:offline-event"),
        "reserve_threads_chain_append": AsyncMock(return_value=SimpleNamespace(reply_to_id=None)),
        "block_threads_chain_append": AsyncMock(),
        publisher: Mock(side_effect=error_type("offline container connection failed")),
    }
    with ExitStack() as stack:
        for name, value in replacements.items():
            stack.enter_context(patch.object(main, name, value))
        deliver = main._deliver_instagram_result if platform == "instagram" else main._deliver_threads_result
        assert deliver(pending, None) == "uncertain"
        main.delete_result_delivery.assert_awaited_once_with(pending)
        main.mark_delivery_claim_uncertain.assert_awaited_once()
        main.release_delivery_claim.assert_not_awaited()
        if platform == "threads":
            main.block_threads_chain_append.assert_awaited_once()


@pytest.mark.parametrize("publication_type", ["tournament_standings", "tournament_vrs_standings"])
def test_review_reproduces_confirmed_telegram_album_missing_from_journal(publication_type):
    match = SimpleNamespace(
        match_uid="audit-final", tournament_parent="audit-event", tournament_name="Audit Event",
        tournament_placements=[
            TournamentPlacement(placement="1", team_name="A", prize_usd=1),
            TournamentPlacement(placement="2", team_name="B", prize_usd=1),
        ],
    )
    pending = SimpleNamespace(
        match=match, channel_id="global", generation=0,
        vrs_impacts=[SimpleNamespace(source="offline-source")],
    )
    replacements = {
        "_hold_unconfirmed_final_outbox": Mock(return_value=False),
        "_delivery_is_current": Mock(return_value=True),
        "_can_publish_tournament_standings": Mock(return_value=True),
        "can_render_tournament_vrs": Mock(return_value=True),
        "_vrs_tournament_id": Mock(return_value="audit-event"),
        "format_tournament_vrs": Mock(return_value="Offline VRS audit"),
        "reconcile_content_delivery": AsyncMock(return_value=False),
        "claim_content_delivery": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_delivery_claim_attempting": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_delivery_claim_sent": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_content_processed": AsyncMock(),
        "delete_result_delivery": AsyncMock(),
        "send_to_telegram": Mock(),
        "_record_post_analytics": Mock(),
        "record_publication": AsyncMock(),
        "TELEGRAM_MEDIA_CARDS": False,
    }
    with ExitStack() as stack:
        for name, value in replacements.items():
            stack.enter_context(patch.object(main, name, value))
        deliver = (main._deliver_tournament_standings if publication_type == "tournament_standings"
                   else main._deliver_tournament_vrs)
        assert deliver(pending, {"chat_id": "offline-audit"}, "Audit") == "sent"
        main.send_to_telegram.assert_called_once()
        main.mark_delivery_claim_sent.assert_awaited_once()
        main.mark_content_processed.assert_awaited_once()
        main.delete_result_delivery.assert_awaited_once()
        main._record_post_analytics.assert_not_called()
        main.record_publication.assert_not_awaited()
