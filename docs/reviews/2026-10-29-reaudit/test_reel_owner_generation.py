"""Observed unresolved owner contract: an old Reel resumes after off/on.

This proof intentionally asserts current incorrect behavior. All external I/O
is replaced with FakeS3, a status stub, and a publication recorder.
"""
import asyncio
import os
import json
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPOSITORY = Path(os.getenv("CS2_REVIEW_SOURCE", "/Users/user/.codex/worktrees/review-fixes-20261029/CS2results"))
sys.path.insert(0, str(REPOSITORY))

from cs2bot import main, publication_admin, reel_delivery
from cs2bot.match_sources import storage


def test_saved_processing_reel_is_published_after_owner_off_on(monkeypatch):
    fake_s3 = runpy.run_path(str(REPOSITORY / "tests/test_storage.py"))["FakeS3"]()
    for module in (storage, publication_admin):
        monkeypatch.setattr(module, "_client", lambda: fake_s3)
        monkeypatch.setattr(module, "_bucket", lambda: "offline-bucket")
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: True)
    monkeypatch.setattr(main, "instagram_reels_enabled", lambda: True)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_notify_admin", lambda *args, **kwargs: None)

    def forbid_network(*args, **kwargs):
        raise AssertionError("network is forbidden by this offline proof")

    monkeypatch.setattr(requests.sessions.Session, "request", forbid_network)
    now = datetime(2026, 10, 29, 6, 15, tzinfo=timezone.utc)
    advance_today = reel_delivery.advance_today_reel
    monkeypatch.setattr(main, "advance_today_reel", lambda context: advance_today(context, now=now))
    status = ["IN_PROGRESS"]
    monkeypatch.setattr(reel_delivery, "reel_container_status", lambda *args: status[0])
    published = []
    monkeypatch.setattr(reel_delivery, "publish_reel_container",
                        lambda container_id, context: published.append(container_id))

    def update(enabled, revision, operation):
        return asyncio.run(publication_admin.update_policy(
            "instagram", "schedule_reel", enabled, actor_user_id=7,
            operation_id=operation, expected_revision=revision,
            client=fake_s3, bucket="offline-bucket", now=now,
        ))

    initial = update(True, 0, "initial")
    assert publication_admin.policy_status(initial, "instagram", "schedule_reel") == (True, 0)
    saved = reel_delivery.save_pending_reel("2026-10-29", "123456", 4)
    assert main._retry_reel_worker(None) == {"2026-10-29": "processing"}
    assert published == []

    paused = update(False, 1, "pause")
    assert publication_admin.policy_status(paused, "instagram", "schedule_reel") == (False, 1)
    status[0] = "FINISHED"
    assert main._retry_reel_worker(None) == {}
    assert published == []

    resumed = update(True, 2, "resume")
    assert publication_admin.policy_status(resumed, "instagram", "schedule_reel") == (True, 1)
    assert reel_delivery.load_pending_reel("2026-10-29") == saved
    outcome = main._retry_reel_worker(None)
    # The old container is published despite predating the owner's new generation.
    assert outcome == {"2026-10-29": "published"}
    assert published == ["123456"]
    assert "generation" not in saved.__dict__
    print(json.dumps({"before_pause": "processing", "during_pause": "held",
                      "generation_after_resume": 1, "old_container": saved.container_id,
                      "after_resume": outcome, "recorded_external_publishes": published,
                      "real_network_requests": 0}, ensure_ascii=False))
