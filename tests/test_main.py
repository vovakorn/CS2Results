import json

from cs2bot import main
from cs2bot.match_sources.models import MatchNormalized


def _match(match_id="1", team1="NAVI", team2="FaZe"):
    return MatchNormalized(
        source="cs2api",
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


def test_format_match_uses_normalized_fields():
    text = main.format_match(_match())
    assert "NAVI vs FaZe" in text
    assert "Score: 2:1" in text
    assert "Event: IEM Cologne 2026" in text
    assert "Match ID: 1" in text


def test_handler_dry_run_does_not_send_or_mark(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is True
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
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
    assert sent == []
    assert marked == []


def test_handler_marks_processed_after_successful_send(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["dry_run"] is False
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))
        return {"ok": True}

    async def fake_is_processed(match_uid):
        return False

    async def fake_mark(match, channel_name):
        marked.append((match.match_uid, channel_name))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "is_processed", fake_is_processed)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["messages_sent"] == 1
    assert len(sent) == 1
    assert marked == [("cs2api_1", "global")]


def test_handler_skips_channel_duplicate(monkeypatch):
    sent = []
    marked = []

    async def fake_get_new_finished_matches(**kwargs):
        assert kwargs["check_processed"] is False
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        sent.append((chat_id, text))

    async def fake_is_processed(match_uid):
        return match_uid == "global_cs2api_1"

    async def fake_mark(match, channel_name):
        marked.append((match.match_uid, channel_name))

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "is_processed", fake_is_processed)
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

    async def fake_get_new_finished_matches(**kwargs):
        return [_match()]

    def fake_send(chat_id, text, timeout=7):
        raise RuntimeError("telegram failed")

    async def fake_is_processed(match_uid):
        return False

    async def fake_mark(match, channel_name):
        marked.append(match.match_uid)

    monkeypatch.setattr(main, "CHANNELS", [{"name": "global", "chat_id": "chat", "teams": None}])
    monkeypatch.setattr(main, "get_new_finished_matches", fake_get_new_finished_matches)
    monkeypatch.setattr(main, "send_to_telegram", fake_send)
    monkeypatch.setattr(main, "is_processed", fake_is_processed)
    monkeypatch.setattr(main, "mark_channel_processed", fake_mark)

    response = main.handler({"limit": 1}, None)

    assert response["statusCode"] == 502
    assert marked == []
