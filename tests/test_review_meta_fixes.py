"""Regressions for preparation failures versus ambiguous external publication."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from cs2bot import instagram_publish, main, threads_publish


@pytest.mark.parametrize("module,create_endpoint,publish_endpoint,safe_error,uncertain_error", [
    (instagram_publish, "media", "media_publish", instagram_publish.InstagramPublishError,
     instagram_publish.InstagramDeliveryUncertainError),
    (threads_publish, "threads", "threads_publish", threads_publish.ThreadsPublishError,
     threads_publish.ThreadsDeliveryUncertainError),
])
@pytest.mark.parametrize("failure", ["connection", "server", "invalid_json", "invalid_payload"])
def test_meta_preparation_failure_is_safe_but_publish_failure_is_uncertain(
    monkeypatch, module, create_endpoint, publish_endpoint, safe_error, uncertain_error, failure,
):
    class Response:
        status_code = 503 if failure == "server" else 200

        def json(self):
            if failure == "invalid_json":
                raise requests.JSONDecodeError("invalid", "x", 0)
            return []

    def post(*args, **kwargs):
        if failure == "connection":
            raise requests.ConnectTimeout("offline connection failure")
        return Response()

    monkeypatch.setattr(module.requests, "post", post)
    with pytest.raises(safe_error) as caught:
        module._meta_post(f"https://example.invalid/user/{create_endpoint}", {}, None)
    assert not isinstance(caught.value, uncertain_error)
    assert "before publication" in str(caught.value)
    with pytest.raises(uncertain_error, match="outcome is unknown"):
        module._meta_post(f"https://example.invalid/user/{publish_endpoint}", {}, None)


@pytest.mark.parametrize("platform", ["instagram", "threads"])
@pytest.mark.parametrize("phase", ["create", "publish"])
def test_result_caller_keeps_safe_preparation_retry_and_blocks_ambiguous_publish(
    monkeypatch, platform, phase,
):
    module = instagram_publish if platform == "instagram" else threads_publish
    credential_name = "_instagram_credentials" if platform == "instagram" else "_threads_credentials"
    create_endpoint = "media" if platform == "instagram" else "threads"
    publish_endpoint = "media_publish" if platform == "instagram" else "threads_publish"
    match = SimpleNamespace(match_uid="offline-match", source="pandascore")
    pending = SimpleNamespace(match=match, channel_id=platform, generation=0, attempt_count=0)
    claim = SimpleNamespace(key="offline-claim")
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"id": "offline-container"}

    def post(url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if phase == "create" or endpoint == publish_endpoint:
            raise requests.ConnectTimeout("offline connection failure")
        return Response()

    monkeypatch.setattr(module, "upload_public_cards", lambda *args: ["https://example.invalid/card.png"])
    monkeypatch.setattr(module, credential_name, lambda context: ("offline-token", "offline-user"))
    monkeypatch.setattr(module, "_meta_proxy", lambda: nullcontext(None))
    monkeypatch.setattr(module.requests, "post", post)
    if platform == "threads":
        monkeypatch.setattr(module, "_wait_for_containers", lambda *args: None)
    monkeypatch.setattr(main, "_delivery_is_current", Mock(return_value=True))
    monkeypatch.setattr(main, "reconcile_channel_delivery", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "claim_channel_delivery", AsyncMock(return_value=claim))
    monkeypatch.setattr(main, "render_result_card", Mock(return_value=b"offline-png"))
    monkeypatch.setattr(main, "format_match", Mock(return_value="Offline result"))
    monkeypatch.setattr(main, "mark_delivery_claim_attempting", AsyncMock(return_value=claim))
    monkeypatch.setattr(main, "mark_delivery_claim_uncertain", AsyncMock())
    monkeypatch.setattr(main, "delete_result_delivery", AsyncMock())
    monkeypatch.setattr(main, "release_delivery_claim", AsyncMock())
    monkeypatch.setattr(main, "record_result_delivery_attempt",
                        AsyncMock(return_value=SimpleNamespace(attempt_count=1)))
    monkeypatch.setattr(main, "_notify_admin", Mock())
    monkeypatch.setattr(main, "_threads_tournament_key", Mock(return_value="threads:offline-event"))
    append = SimpleNamespace(reply_to_id=None)
    monkeypatch.setattr(main, "reserve_threads_chain_append", AsyncMock(return_value=append))
    monkeypatch.setattr(main, "release_threads_chain_append", AsyncMock())
    monkeypatch.setattr(main, "block_threads_chain_append", AsyncMock())

    deliver = main._deliver_instagram_result if platform == "instagram" else main._deliver_threads_result
    if phase == "create":
        assert deliver(pending, None) == "failed"
        assert calls == [create_endpoint]
        main.delete_result_delivery.assert_not_awaited()
        main.release_delivery_claim.assert_awaited_once_with(claim)
        main.record_result_delivery_attempt.assert_awaited_once_with(pending)
        main.mark_delivery_claim_uncertain.assert_not_awaited()
        if platform == "threads":
            main.release_threads_chain_append.assert_awaited_once_with(append)
            main.block_threads_chain_append.assert_not_awaited()
    else:
        assert deliver(pending, None) == "uncertain"
        assert calls == [create_endpoint, publish_endpoint]
        main.delete_result_delivery.assert_awaited_once_with(pending)
        main.release_delivery_claim.assert_not_awaited()
        main.mark_delivery_claim_uncertain.assert_awaited_once_with(claim)
        if platform == "threads":
            main.block_threads_chain_append.assert_awaited_once_with(append)
            main.release_threads_chain_append.assert_not_awaited()
