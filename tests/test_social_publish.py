import json

import pytest

from cs2bot import social_publish


def test_iam_token_reads_yandex_context_access_token():
    assert social_publish._iam_token({"token": {"access_token": "iam-token"}}) == "iam-token"


def test_publish_creates_then_publishes_container(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_LOCKBOX_SECRET_ID", "secret-id")
    monkeypatch.setattr(
        social_publish,
        "_instagram_credentials",
        lambda context: ("access-token", "user-id"),
    )

    class XrayContext:
        def __enter__(self):
            return {"http": "http://127.0.0.1:12345", "https": "http://127.0.0.1:12345"}

        def __exit__(self, *args):
            return False

    calls = []

    def fake_post(url, *, data, proxy):
        calls.append((url, data, proxy))
        return {"id": "container-id"} if url.endswith("/media") else {"id": "media-id"}

    monkeypatch.setattr(social_publish, "xray_http_proxy", lambda: XrayContext())
    monkeypatch.setattr(social_publish, "_meta_post", fake_post)

    assert social_publish.publish_instagram_test_post(None) == {"media_id": "media-id"}
    assert calls[0][0].endswith("/user-id/media")
    assert calls[1][0].endswith("/user-id/media_publish")
    assert "access-token" not in json.dumps(calls[0][0])


def test_threads_publish_creates_then_publishes_container(monkeypatch):
    monkeypatch.setattr(
        social_publish,
        "_threads_credentials",
        lambda context: ("threads-access-token", "threads-user-id"),
    )

    class XrayContext:
        def __enter__(self):
            return {"http": "http://127.0.0.1:12345", "https": "http://127.0.0.1:12345"}

        def __exit__(self, *args):
            return False

    calls = []

    def fake_post(url, *, data, proxy):
        calls.append((url, data, proxy))
        return {"id": "container-id"} if url.endswith("/threads") else {"id": "post-id"}

    monkeypatch.setattr(social_publish, "xray_http_proxy", lambda: XrayContext())
    monkeypatch.setattr(social_publish, "_meta_post", fake_post)

    assert social_publish.publish_threads_test_post(None) == {"post_id": "post-id"}
    assert calls[0][0].endswith("/threads-user-id/threads")
    assert calls[0][1]["media_type"] == "TEXT"
    assert calls[1][0].endswith("/threads-user-id/threads_publish")
    assert calls[1][1]["creation_id"] == "container-id"
    assert all(proxy == calls[0][2] for _, _, proxy in calls)


def test_threads_publish_refuses_missing_container_id(monkeypatch):
    monkeypatch.setattr(social_publish, "_threads_credentials", lambda context: ("token", "user-id"))

    class XrayContext:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(social_publish, "xray_http_proxy", lambda: XrayContext())
    monkeypatch.setattr(social_publish, "_meta_post", lambda *args, **kwargs: {})

    with pytest.raises(social_publish.SocialPublishError, match="did not create"):
        social_publish.publish_threads_test_post(None)


def test_handler_dispatches_threads_test_job(monkeypatch):
    monkeypatch.setattr(social_publish, "publish_threads_test_post", lambda context: {"post_id": "post-id"})

    response = social_publish.handler({"job": "threads_test_post"}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True, "post_id": "post-id"}


def test_threads_test_card_uses_threads_card_publisher(monkeypatch):
    seen = {}

    def fake_publish(publication_key, cards, caption, context):
        seen.update(
            publication_key=publication_key,
            card=cards[0],
            caption=caption,
            context=context,
        )
        return "post-id"

    monkeypatch.setattr(social_publish, "publish_threads_rendered_cards", fake_publish)

    assert social_publish.publish_threads_test_card({"token": "iam"}) == {"post_id": "post-id"}
    assert seen["publication_key"] == social_publish.THREADS_TEST_CARD_KEY
    assert seen["card"].startswith(b"\x89PNG")
    assert seen["caption"] == social_publish.THREADS_TEST_CARD_CAPTION


def test_handler_dispatches_threads_test_card(monkeypatch):
    monkeypatch.setattr(social_publish, "publish_threads_test_card", lambda context: {"post_id": "post-id"})

    response = social_publish.handler({"job": "threads_test_card"}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True, "post_id": "post-id"}


def test_handler_rejects_non_test_job():
    assert social_publish.handler({}, None)["statusCode"] == 404
