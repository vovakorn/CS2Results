import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from cs2bot import monthly_vrs_reels as monthly
from cs2bot.instagram_publish import InstagramDeliveryUncertainError

NOW = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
STATE = {"month": "2026-10", "generation": 0, "container_id": "12345"}


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected side effect")


def async_value(value):
    async def call(*args, **kwargs):
        return value
    return call


def receipt():
    digest = hashlib.sha256(b"video").hexdigest()
    source = "a" * 64
    return {"month": "2026-10", "generation": 0, "render": {
        "month": "2026-10", "dry_run": False, "caption": "Изменения VRS по итогам октября 2026 года.",
        "sourceKey": f"vrs-monthly/2026-10/sources/{source}.json",
        "artifacts": {"videoKey": f"vrs-monthly/2026-10/renders/world/{source}/{digest}/reel.mp4"},
        "render": {"sha256": digest, "bytes": 5, "width": 1080, "height": 1920, "fps": 60,
                   "frames": 1944, "fullDecode": "passed"}}}


def test_month_rollover_uses_moscow_not_host_timezone():
    assert monthly.previous_month(datetime(2026, 12, 31, 21, tzinfo=timezone.utc)) == "2026-12"
    assert monthly.previous_month(datetime(2026, 12, 31, 20, 59, tzinfo=timezone.utc)) == "2026-11"
    for month in ["2026-11", "2026-13", "2026-1", "../"]:
        with pytest.raises(ValueError):
            monthly.validate_month(month, NOW)


def test_dry_run_is_read_only_and_never_creates_a_post(monkeypatch):
    monkeypatch.setattr(monthly, "_policy", forbidden)
    monkeypatch.setattr(monthly, "prepare", forbidden)
    monkeypatch.setattr(monthly, "request_render", forbidden)
    result = monthly.handler({"job": "monthly_vrs"}, None, now=NOW)
    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["month"] == "2026-10" and body["scope"] == "world" and body["dry_run"] is True


def test_source_probe_reports_owner_readiness_without_creating_a_post(monkeypatch):
    monkeypatch.setattr(monthly, "request_render", lambda *args, **kwargs: {"month": "2026-10", "dry_run": True})
    monkeypatch.setattr(monthly, "_enabled", lambda: True)
    monkeypatch.setattr(monthly, "_policy", lambda: (False, 0))
    monkeypatch.setattr(monthly, "prepare", forbidden)
    result = monthly.handler({"job": "monthly_vrs", "inspect_source": True}, None, now=NOW)
    assert json.loads(result["body"])["readiness"] == {"publishing_enabled": True, "owner_allowed": False, "generation": 0}


@pytest.mark.parametrize("event", [
    {"job": "monthly_vrs", "month": "2026-10", "dry_run": False},
    {"job": "monthly_vrs", "month": "2026-11"},
    {"job": "monthly_vrs", "dry_run": "false"},
    {"job": "monthly_vrs", "inspect_source": "yes"},
])
def test_invalid_requests_and_historical_publications_stop_before_effects(monkeypatch, event):
    monkeypatch.setattr(monthly, "_enabled", forbidden)
    assert monthly.handler(event, None, now=NOW)["statusCode"] == 400


def test_disabled_and_off_window_do_not_read_storage_or_render(monkeypatch):
    monkeypatch.setattr(monthly, "_policy", forbidden)
    monkeypatch.setattr(monthly, "prepare", forbidden)
    monkeypatch.delenv("ENABLE_MONTHLY_VRS_REELS", raising=False)
    response = monthly.handler({"job": "monthly_vrs", "dry_run": False}, None, now=NOW)
    assert json.loads(response["body"])["state"] == "disabled"
    monkeypatch.setattr(monthly, "_enabled", forbidden)
    response = monthly.handler({"job": "monthly_vrs", "dry_run": False}, None,
        now=datetime(2026, 11, 4, 9, tzinfo=timezone.utc))
    assert json.loads(response["body"])["state"] == "outside_window"


def test_timer_retries_do_not_republish_completed_month(monkeypatch):
    monkeypatch.setattr(monthly, "_enabled", lambda: True)
    monkeypatch.setattr(monthly, "_policy", lambda: (True, 0))
    monkeypatch.setattr(monthly.storage, "is_processed", async_value(True))
    monkeypatch.setattr(monthly, "prepare", forbidden)
    event = {"messages": [{"details": {"payload": json.dumps({"job": "monthly_vrs", "dry_run": False})}}]}
    for _ in range(2):
        response = monthly.handler(event, None, now=NOW)
        assert json.loads(response["body"])["state"] == "already_sent"


def test_receipt_rejects_other_month_or_bucket_scope_and_incomplete_render():
    assert monthly._receipt(receipt(), "2026-10")["month"] == "2026-10"
    for mutate in [
        lambda r: r["render"]["artifacts"].update(videoKey="claims/private.json"),
        lambda r: r["render"]["render"].update(fullDecode="failed"),
        lambda r: r.update(month="2026-09"),
        lambda r: r["render"]["render"].update(sha256="b" * 64),
    ]:
        data = receipt(); mutate(data)
        with pytest.raises(ValueError):
            monthly._receipt(data, "2026-10")


def test_persisted_pending_container_is_never_rerendered(monkeypatch):
    monkeypatch.setattr(monthly, "_read", lambda key: STATE)
    monkeypatch.setattr(monthly, "request_render", forbidden)
    monkeypatch.setattr(monthly.instagram, "create_reel_container", forbidden)
    assert monthly.prepare("2026-10", None, 0) == STATE


def test_prepared_video_is_reused_after_container_creation_failure(monkeypatch):
    monkeypatch.setattr(monthly, "_read", lambda key: receipt() if "/prepared/" in key else None)
    monkeypatch.setattr(monthly.storage, "claim_content_delivery", async_value(object()))
    monkeypatch.setattr(monthly.storage, "release_delivery_claim", async_value(None))
    monkeypatch.setattr(monthly, "_policy", lambda: (True, 0))
    monkeypatch.setattr(monthly, "request_render", forbidden)
    monkeypatch.setattr(monthly, "_video", lambda r: b"video")
    monkeypatch.setattr(monthly.instagram, "upload_public_reel", lambda *a: "https://media/reel.mp4")
    monkeypatch.setattr(monthly.instagram, "create_reel_container", lambda *a: "9876")
    monkeypatch.setattr(monthly, "_first_write", lambda key, state: state)
    assert monthly.prepare("2026-10", None, 0)["container_id"] == "9876"


def test_conditional_container_state_preserves_first_writer(monkeypatch):
    class S3:
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    monkeypatch.setattr(monthly.storage, "_client", S3)
    monkeypatch.setattr(monthly.storage, "_bucket", lambda: "private")
    monkeypatch.setattr(monthly, "_read", lambda key: STATE)
    assert monthly._first_write("vrs-monthly/pending/2026-10.json", {**STATE, "container_id": "999"}) == STATE


def test_invalid_render_is_not_frozen_into_a_permanent_receipt(monkeypatch):
    monkeypatch.setattr(monthly, "_read", lambda key: None)
    monkeypatch.setattr(monthly.storage, "claim_content_delivery", async_value(object()))
    monkeypatch.setattr(monthly.storage, "release_delivery_claim", async_value(None))
    monkeypatch.setattr(monthly, "request_render", lambda *args, **kwargs: {"month": "2026-10", "dry_run": False})
    monkeypatch.setattr(monthly, "_first_write", forbidden)
    with pytest.raises(ValueError, match="receipt"):
        monthly.prepare("2026-10", None, 0)


@pytest.fixture
def delivery(monkeypatch):
    events = []
    monkeypatch.setattr(monthly, "_policy", lambda: (True, 0))
    monkeypatch.setattr(monthly.storage, "reconcile_content_delivery", async_value(False))
    monkeypatch.setattr(monthly.storage, "claim_content_delivery", async_value("claim"))
    for method in ["mark_delivery_claim_attempting", "mark_delivery_claim_sent", "mark_delivery_claim_uncertain",
                   "mark_content_processed", "release_delivery_claim"]:
        async def call(*args, _method=method, **kwargs):
            events.append(_method)
            return "claim"
        monkeypatch.setattr(monthly.storage, method, call)
    monkeypatch.setattr(monthly, "record_publication", async_value(True))
    monkeypatch.setattr(monthly.instagram, "reel_container_status", lambda *args: "FINISHED")
    monkeypatch.setattr(monthly.instagram, "publish_reel_container", lambda *args: events.append("publish") or "321")
    return events


def test_publish_is_fenced_and_completion_only_follows_confirmation(delivery):
    assert monthly.advance(STATE, None) == "published"
    assert delivery == ["mark_delivery_claim_attempting", "publish", "mark_delivery_claim_sent", "mark_content_processed"]


def test_ambiguous_publish_blocks_automatic_retries(monkeypatch, delivery):
    def ambiguous(*args):
        raise InstagramDeliveryUncertainError("ambiguous")
    monkeypatch.setattr(monthly.instagram, "publish_reel_container", ambiguous)
    assert monthly.advance(STATE, None) == "publish_uncertain"
    assert delivery == ["mark_delivery_claim_attempting", "mark_delivery_claim_uncertain"]
    monkeypatch.setattr(monthly.storage, "claim_content_delivery", async_value(None))
    monkeypatch.setattr(monthly.instagram, "reel_container_status", forbidden)
    assert monthly.advance(STATE, None) == "duplicate_or_blocked"


def test_processing_resumes_same_container_and_pause_is_rechecked_before_publish(monkeypatch, delivery):
    monkeypatch.setattr(monthly.instagram, "reel_container_status", lambda *args: "IN_PROGRESS")
    assert monthly.advance(STATE, None) == "processing"
    assert delivery == ["release_delivery_claim"]
    delivery.clear()
    answers = iter([(True, 0), (False, 1)])
    monkeypatch.setattr(monthly, "_policy", lambda: next(answers))
    monkeypatch.setattr(monthly.instagram, "reel_container_status", lambda *args: "FINISHED")
    assert monthly.advance(STATE, None) == "held_or_stale"
    assert delivery == ["release_delivery_claim"]


def test_old_generation_never_reopens_after_owner_reenables(monkeypatch, delivery):
    monkeypatch.setattr(monthly, "_policy", lambda: (True, 1))
    monkeypatch.setattr(monthly.instagram, "reel_container_status", forbidden)
    assert monthly.advance(STATE, None) == "held_or_stale"
    assert not delivery


def test_private_download_must_match_render_checksum(monkeypatch):
    class S3:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"wrong")}
    monkeypatch.setattr(monthly.storage, "_client", S3)
    monkeypatch.setattr(monthly.storage, "_bucket", lambda: "private")
    with pytest.raises(ValueError, match="checksum"):
        monthly._video(receipt()["render"])


def test_renderer_uses_private_iam_production_tag_and_read_only_source_probe(monkeypatch):
    monkeypatch.setenv("VRS_RENDER_FUNCTION_ID", "d4e8n07roso4762pf1h4")
    monkeypatch.setattr(monthly.instagram, "_iam_token", lambda context: "private-token")
    class Response:
        content = b'{}'
        def raise_for_status(self):
            pass
        def json(self):
            return {"month": "2026-10", "dry_run": True}
    def post(url, **kwargs):
        assert url == "https://functions.yandexcloud.net/d4e8n07roso4762pf1h4"
        assert kwargs["params"] == {"tag": "production"}
        assert kwargs["headers"] == {"Authorization": "Bearer private-token"}
        assert kwargs["json"] == {"job": "monthly", "month": "2026-10", "dry_run": True}
        assert kwargs["allow_redirects"] is False
        return Response()
    monkeypatch.setattr(monthly.requests, "post", post)
    assert monthly.request_render("2026-10", None, dry_run=True)["dry_run"] is True
