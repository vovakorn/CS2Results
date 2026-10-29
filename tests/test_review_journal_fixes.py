"""Regression coverage for Telegram journal writes and recent bounded reads."""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cs2bot import admin_handler as admin, main, publication_admin as journal
from cs2bot.match_sources.models import TournamentPlacement
from cs2bot.match_sources.storage import StorageUnavailableError


@pytest.mark.parametrize("publication_type", ["tournament_standings", "tournament_vrs_standings"])
@pytest.mark.parametrize("journal_unavailable", [False, True])
def test_confirmed_telegram_album_records_journal_without_retrying_on_journal_error(
    monkeypatch, publication_type, journal_unavailable,
):
    match = SimpleNamespace(
        match_uid="offline-final", tournament_parent="offline-event", tournament_name="Offline Event",
        tournament_placements=[
            TournamentPlacement(placement="1", team_name="A", prize_usd=1),
            TournamentPlacement(placement="2", team_name="B", prize_usd=1),
        ],
    )
    pending = SimpleNamespace(match=match, channel_id="global", generation=1,
                              vrs_impacts=[SimpleNamespace(source="offline-source")])
    replacements = {
        "_hold_unconfirmed_final_outbox": Mock(return_value=False),
        "_delivery_is_current": Mock(return_value=True),
        "_can_publish_tournament_standings": Mock(return_value=True),
        "can_render_tournament_vrs": Mock(return_value=True),
        "_vrs_tournament_id": Mock(return_value="offline-event"),
        "format_tournament_vrs": Mock(return_value="Offline VRS"),
        "reconcile_content_delivery": AsyncMock(return_value=False),
        "claim_content_delivery": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_delivery_claim_attempting": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_delivery_claim_sent": AsyncMock(return_value=SimpleNamespace(key="claim")),
        "mark_content_processed": AsyncMock(),
        "delete_result_delivery": AsyncMock(),
        "release_delivery_claim": AsyncMock(),
        "send_to_telegram": Mock(),
        "record_post": AsyncMock(),
        "record_publication": AsyncMock(side_effect=StorageUnavailableError("offline") if journal_unavailable else None),
        "TELEGRAM_MEDIA_CARDS": False,
    }
    for name, value in replacements.items():
        monkeypatch.setattr(main, name, value)
    deliver = (main._deliver_tournament_standings if publication_type == "tournament_standings"
               else main._deliver_tournament_vrs)
    assert deliver(pending, {"chat_id": "offline"}, "Offline") == "sent"
    main.send_to_telegram.assert_called_once()
    main.mark_delivery_claim_sent.assert_awaited_once()
    main.delete_result_delivery.assert_awaited_once()
    main.release_delivery_claim.assert_not_awaited()
    main.record_post.assert_awaited_once()
    main.record_publication.assert_awaited_once()
    assert main.record_publication.call_args.args[1:4] == ("global", "telegram", publication_type)
    assert main.record_publication.call_args.kwargs["generation"] == pending.generation


class JournalS3:
    """Ordered S3 listings with real metadata pagination and observable GETs."""

    def __init__(self, page_size=100):
        self.objects = {}
        self.page_size = page_size
        self.list_calls = []
        self.reads = []

    def add(self, name, confirmed, *, modified=None):
        key = f"{journal.JOURNAL_PREFIX}{confirmed.date().isoformat()}/{name}.json"
        self.objects[key] = {
            "body": json.dumps({"publication_id": name, "destination_id": "global",
                                "publication_type": "results", "confirmed_at": confirmed.isoformat()}).encode(),
            "modified": modified or confirmed,
        }
        return key

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        keys = sorted(key for key in self.objects
                      if key.startswith(kwargs["Prefix"]) and key > kwargs.get("StartAfter", ""))
        offset = int(kwargs.get("ContinuationToken", "0"))
        stop = min(offset + self.page_size, offset + kwargs["MaxKeys"], len(keys))
        return {"Contents": [{"Key": key, "LastModified": self.objects[key]["modified"]}
                             for key in keys[offset:stop]],
                "IsTruncated": stop < len(keys), "NextContinuationToken": str(stop)}

    def get_object(self, **kwargs):
        self.reads.append(kwargs["Key"])
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]]["body"])}


def test_today_read_skips_501_old_bodies_and_includes_new_publication():
    s3 = JournalS3()
    today = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
    for index in range(501):
        s3.add(f"old-{index:03d}", today - timedelta(days=2))
    fresh_key = s3.add("fresh", today)
    since = journal.period_start("today", now=today)
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", since=since))
    assert [item["publication_id"] for item in records] == ["fresh"]
    assert s3.reads == [fresh_key]
    assert s3.list_calls[0]["StartAfter"] == f"{journal.JOURNAL_PREFIX}2026-10-28/"


def test_metadata_order_selects_recent_records_before_body_limit():
    s3 = JournalS3(page_size=1)
    today = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
    s3.add("z-old", today)
    fresh = s3.add("a-new", today + timedelta(minutes=1))
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", limit=1))
    assert [item["publication_id"] for item in records] == ["a-new"]
    assert len(s3.list_calls) == 2
    assert s3.reads == [fresh]


def test_missing_last_modified_uses_stable_key_order_before_body_limit():
    s3 = JournalS3(page_size=1)
    today = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
    for name in ("a", "z"):
        key = s3.add(name, today)
        s3.objects[key]["modified"] = None
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", limit=1))
    assert [item["publication_id"] for item in records] == ["z"]
    assert len(s3.reads) == 1


def test_moscow_midnight_is_filtered_after_utc_day_listing():
    s3 = JournalS3()
    before = datetime(2026, 10, 28, 20, 59, 59, tzinfo=timezone.utc)
    start = before + timedelta(seconds=1)
    s3.add("before-moscow-day", before)
    s3.add("moscow-day", start)
    records = asyncio.run(journal.list_publications(
        client=s3, bucket="offline", since=journal.period_start("today", now=start)))
    assert [item["publication_id"] for item in records] == ["moscow-day"]
    assert s3.list_calls[0]["StartAfter"].endswith("2026-10-28/")


def test_more_than_500_period_records_stays_bounded_and_discloses_incomplete_counts():
    s3 = JournalS3()
    now = datetime.now(timezone.utc)
    start = journal.period_start("today", now=now).astimezone(timezone.utc)
    for index in range(501):
        s3.add(f"record-{index:03d}", start + timedelta(seconds=index))
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline", since=start))
    assert len(records) == len(s3.reads) == 500
    assert records[0]["publication_id"] == "record-500"
    assert records[-1]["publication_id"] == "record-001"
    policy = journal.PolicySnapshot(1, {}, None)
    for text in (admin._overview(records, [], policy, "today"),
                 admin._journal_text(records, [], policy, "today", 0),
                 admin._pair_detail(records, [], policy, "global", "results")):
        assert "последним 500 доступным записям периода" in text
        assert "могут быть неполными" in text


def test_pair_detail_labels_last_publication_as_period_scoped():
    policy = journal.PolicySnapshot(1, {}, None)
    records = [{"publication_id": "old", "destination_id": "global", "publication_type": "results",
                "confirmed_at": "2020-01-01T00:00:00Z"}]
    text = admin._pair_detail(records, [], policy, "global", "results")
    assert "Последняя публикация в выбранном периоде: нет подтверждённой публикации" in text
    assert "2020-01-01" not in text


@pytest.mark.parametrize("callback,expected_period", [
    ("a:o:week", "week"), ("a:j:0:month", "month"), ("a:x:0:0", "today"),
])
def test_admin_reads_requested_period_with_the_existing_500_body_limit(monkeypatch, callback, expected_period):
    from test_admin_handler import _event

    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(admin, "CHANNELS", [{"id": "global", "name": "Global", "chat_id": "offline"}])
    monkeypatch.setattr(admin, "read_policy", AsyncMock(return_value=journal.PolicySnapshot(1, {}, None)))
    monkeypatch.setattr(admin, "telegram_call", Mock())
    monkeypatch.setattr(admin, "_send_or_edit", Mock())
    monkeypatch.setattr(admin, "_pending", lambda: [])
    starts = {period: journal.period_start(period) for period in admin.PERIODS}
    monkeypatch.setattr(admin, "period_start", lambda period: starts[period])
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(admin, "list_publications", loader)
    assert admin.handler(_event(callback, callback=True), None)["statusCode"] == 200
    loader.assert_awaited_once_with(limit=500, since=starts[expected_period])
