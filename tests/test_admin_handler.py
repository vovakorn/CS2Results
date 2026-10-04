import asyncio

from cs2bot import admin_handler as admin
from cs2bot.publication_admin import PolicySnapshot


def _event(data="/admin", *, callback=False, secret="secret", user_id=42, chat_type="private"):
    source = {"from": {"id": user_id}, "chat": {"id": 1, "type": chat_type}}
    if callback:
        source.update({"id": "callback", "data": data, "message": {"message_id": 9, "chat": source["chat"]}})
        update = {"callback_query": source}
    else:
        source["text"] = data; update = {"message": source}
    return {"headers": {"X-Telegram-Bot-Api-Secret-Token": secret}, "body": __import__("json").dumps(update)}


def test_admin_rejects_non_owner_group_and_wrong_secret(monkeypatch):
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "secret")
    for event in (_event(user_id=41), _event(chat_type="group"), _event(secret="wrong")):
        assert admin.handler(event, None)["statusCode"] == 403


def test_admin_opens_menu_and_uses_short_callback(monkeypatch):
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(admin, "CHANNELS", [{"id": "a-very-long-channel-identifier-that-does-not-enter-callback", "name": "Global", "chat_id": "-1"}])
    async def policy(): return PolicySnapshot(0, {}, None)
    monkeypatch.setattr(admin, "read_policy", policy)
    sent = []
    monkeypatch.setattr(admin, "_send_or_edit", lambda update, text, markup: sent.append((text, markup)))
    assert admin.handler(_event(), None)["statusCode"] == 200
    assert "админка" in sent[0][0]
    callback = sent[0][1]["inline_keyboard"][0][0]["callback_data"]
    assert len(callback.encode()) <= 64


def test_pair_card_reports_confirmed_count_and_held_queue():
    policy = PolicySnapshot(1, {"channel:results": {"enabled": True, "generation": 1}}, '"1"')
    record = {"publication_id": "one", "destination_id": "channel", "publication_type": "results",
              "confirmed_at": "2026-10-04T09:00:00Z", "test": False}
    pending = type("Pending", (), {"channel_id": "channel", "content_type": "result", "generation": 0,
                                    "attempt_count": 0})()
    text = admin._pair_detail([record], [pending], policy, "channel", "results")
    assert "За сегодня: 1" in text
    assert "ручная проверка" in text
