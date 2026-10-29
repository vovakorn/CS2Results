"""One journal fact per fully confirmed logical schedule context edition."""
import asyncio
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from cs2bot import main, publication_admin as journal
from cs2bot.match_sources import storage
from test_storage import FakeS3


@pytest.fixture
def context_delivery(monkeypatch):
    s3 = FakeS3()
    for module in (storage, journal):
        monkeypatch.setattr(module, "_client", lambda: s3)
        monkeypatch.setattr(module, "_bucket", lambda: "offline")
    monkeypatch.setattr(main, "PUBLICATION_CONTROL_ENABLED", True)
    asyncio.run(journal.update_policy(
        "global", "schedule_context", False, actor_user_id=7, operation_id="off",
        expected_revision=0, client=s3, bucket="offline"))
    asyncio.run(journal.update_policy(
        "global", "schedule_context", True, actor_user_id=7, operation_id="on",
        expected_revision=1, client=s3, bucket="offline"))
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", False)
    text, photo, album = Mock(), Mock(), Mock()
    monkeypatch.setattr(main, "send_to_telegram", text)
    monkeypatch.setattr(main, "send_photo_to_telegram", photo)
    monkeypatch.setattr(main, "send_media_group_to_telegram", album)
    return s3, text, photo, album


def send_context(messages, *, test_run_id=None):
    main._send_schedule_context_to_telegram(
        {"id": "global", "name": "Global", "chat_id": "offline"},
        messages, [object()], datetime.now(ZoneInfo("Europe/Moscow")), test_run_id,
    )


@pytest.mark.parametrize("kind", ["text", "photo", "album", "photo_rejected"])
def test_confirmed_context_is_one_journal_fact_with_original_generation(context_delivery, monkeypatch, kind):
    s3, text, photo, album = context_delivery
    messages = ["Part one", "Part two"] if kind in {"text", "album"} else ["One part"]
    if kind != "text":
        monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
        monkeypatch.setattr(main, "render_schedule_context_covers",
                            lambda *args, **kwargs: [b"one", b"two"] if kind == "album" else [b"one"])
    if kind == "photo_rejected":
        photo.side_effect = main.TelegramDeliveryError("definite rejection")
    send_context(messages)
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline"))
    assert len(records) == 1
    assert records[0]["publication_type"] == "schedule_context"
    assert records[0]["generation"] == 1
    assert records[0]["metadata"]["media_card"] is (kind in {"photo", "album"})
    assert len(journal.publications_in_period(records, "today")) == 1
    assert text.call_count == (0 if kind == "photo" else len(messages))
    assert photo.call_count == (1 if kind in {"photo", "photo_rejected"} else 0)
    assert album.call_count == (1 if kind == "album" else 0)


@pytest.mark.parametrize("error", [main.TelegramDeliveryError, main.TelegramDeliveryUncertainError])
def test_partial_or_uncertain_context_is_not_counted_as_confirmed(context_delivery, error):
    s3, text, _, _ = context_delivery
    text.side_effect = [None, error("part two not confirmed")]
    send_context(["Part one", "Part two"])
    assert text.call_count == 2
    assert asyncio.run(journal.list_publications(client=s3, bucket="offline")) == []


def test_uncertain_cover_has_no_text_retry_or_journal(context_delivery, monkeypatch):
    s3, text, photo, _ = context_delivery
    monkeypatch.setattr(main, "TELEGRAM_MEDIA_CARDS", True)
    monkeypatch.setattr(main, "render_schedule_context_covers", lambda *args, **kwargs: [b"one"])
    photo.side_effect = main.TelegramDeliveryUncertainError("unknown")
    with pytest.raises(main.TelegramDeliveryUncertainError):
        send_context(["One part"])
    text.assert_not_called()
    assert asyncio.run(journal.list_publications(client=s3, bucket="offline")) == []


def test_journal_failure_does_not_repeat_confirmed_context(context_delivery, monkeypatch):
    _, text, _, _ = context_delivery
    async def unavailable(*args, **kwargs):
        raise storage.StorageUnavailableError("journal unavailable")
    monkeypatch.setattr(main, "record_publication", unavailable)
    send_context(["One part"])
    text.assert_called_once()


def test_disabled_context_does_not_send_or_record(context_delivery, monkeypatch):
    s3, text, photo, album = context_delivery
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (False, 2))
    send_context(["One part"])
    assert not any(sender.called for sender in (text, photo, album))
    assert asyncio.run(journal.list_publications(client=s3, bucket="offline")) == []


def test_test_context_is_excluded_from_regular_counts(context_delivery):
    s3, _, _, _ = context_delivery
    send_context(["One part"], test_run_id="review")
    records = asyncio.run(journal.list_publications(client=s3, bucket="offline"))
    assert len(records) == 1 and records[0]["test"] is True
    assert journal.publications_in_period(records, "today") == []
