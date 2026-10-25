import asyncio
from datetime import datetime, timezone

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


def test_callback_is_acknowledged_before_storage_free_navigation(monkeypatch):
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "secret")
    calls, sent = [], []

    def telegram_call(method, payload):
        calls.append((method, payload))
        return {"ok": True}

    async def unavailable_policy():
        raise AssertionError("top-level navigation must not read storage")

    monkeypatch.setattr(admin, "telegram_call", telegram_call)
    monkeypatch.setattr(admin, "read_policy", unavailable_policy)
    monkeypatch.setattr(admin, "_send_or_edit", lambda update, text, markup: sent.append(text))

    assert admin.handler(_event("a:c", callback=True), None)["statusCode"] == 200
    assert calls == [("answerCallbackQuery", {"callback_query_id": "callback"})]
    assert sent == ["<b>Каналы и аккаунты</b>\nВыберите назначение."]


def test_pair_card_reports_confirmed_count_and_held_queue():
    policy = PolicySnapshot(1, {"channel:results": {"enabled": True, "generation": 1}}, '"1"')
    record = {"publication_id": "one", "destination_id": "channel", "publication_type": "results",
              "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "test": False}
    pending = type("Pending", (), {"channel_id": "channel", "content_type": "result", "generation": 0,
                                    "attempt_count": 0})()
    text = admin._pair_detail([record], [pending], policy, "channel", "results")
    assert "За сегодня: 1" in text
    assert "ручная проверка" in text


def test_failed_callback_ack_is_attempted_once_and_does_not_block_edit(monkeypatch):
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_ENABLED", True)
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_USER_ID", "42")
    monkeypatch.setattr(admin, "TELEGRAM_ADMIN_WEBHOOK_SECRET", "secret")
    calls = []

    def call(method, payload):
        calls.append(method)
        if method == "answerCallbackQuery":
            raise RuntimeError("expired acknowledgement")
        return {"ok": True}

    monkeypatch.setattr(admin, "telegram_call", call)
    assert admin.handler(_event("a:c", callback=True), None)["statusCode"] == 200
    assert calls == ["answerCallbackQuery", "editMessageText"]


def test_no_change_response_is_success_only_for_edit(monkeypatch):
    import pytest
    monkeypatch.setattr(admin, "TELEGRAM_TOKEN", "fixture-token")
    monkeypatch.setattr(admin, "TELEGRAM_PROXY_URL", None)

    class Response:
        status_code = 400
        def json(self):
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: message is not modified: specified new message content is the same"}

    monkeypatch.setattr(admin.requests, "post", lambda *args, **kwargs: Response())
    assert admin.telegram_call("editMessageText", {}) == {"ok": True, "unchanged": True}
    with pytest.raises(RuntimeError):
        admin.telegram_call("sendMessage", {})


def test_other_api_errors_remain_errors_and_logs_exclude_response_body(monkeypatch, caplog):
    import pytest
    monkeypatch.setattr(admin, "TELEGRAM_TOKEN", "fixture-token")

    class Response:
        status_code = 400
        def json(self):
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: private-response-value"}

    monkeypatch.setattr(admin.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError):
        admin.telegram_call("editMessageText", {})
    assert "telegram_admin_api_rejected method=editMessageText status=400" in caplog.text
    assert "private-response-value" not in caplog.text
    assert "fixture-token" not in caplog.text


def test_ambiguous_outbound_failure_is_not_retried(monkeypatch):
    import pytest
    monkeypatch.setattr(admin, "TELEGRAM_TOKEN", "fixture-token")
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise admin.requests.ReadTimeout("private-response-value")

    monkeypatch.setattr(admin.requests, "post", post)
    with pytest.raises(RuntimeError, match="telegram admin transport unavailable"):
        admin.telegram_call("sendMessage", {})
    assert len(calls) == 1
