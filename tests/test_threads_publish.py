import pytest
import requests

from cs2bot import threads_publish


def test_upload_public_cards_persists_threads_urls(monkeypatch):
    monkeypatch.setenv("THREADS_MEDIA_BUCKET", "social-media")
    uploaded = []

    class Client:
        def put_object(self, **kwargs):
            uploaded.append(kwargs)

    monkeypatch.setattr(threads_publish, "_media_client", lambda: Client())

    assert threads_publish.upload_public_cards("schedule_2026-08-30", [b"one", b"two"]) == [
        "https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/1.png",
        "https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/2.png",
    ]
    assert all(item["ACL"] == "public-read" for item in uploaded)
    assert all(item["CacheControl"].endswith("immutable") for item in uploaded)


def test_publish_cards_creates_carousel_then_publishes(monkeypatch):
    monkeypatch.setattr(threads_publish, "_threads_credentials", lambda context: ("token", "user"))

    class Proxy:
        def __enter__(self):
            return {"https": "http://127.0.0.1:1000"}

        def __exit__(self, *args):
            return False

    calls = []

    def fake_post(url, data, proxy):
        calls.append((url, data, proxy))
        if url.endswith("/threads_publish"):
            return {"id": "post-id"}
        return {"id": f"container-{len(calls)}"}

    monkeypatch.setattr(threads_publish, "_meta_proxy", lambda: Proxy())
    monkeypatch.setattr(threads_publish, "_meta_post", fake_post)

    assert threads_publish.publish_cards(["https://a/1.png", "https://a/2.png"], "caption", None) == "post-id"
    assert [call[1].get("media_type") for call in calls] == ["IMAGE", "IMAGE", "CAROUSEL", None]
    assert calls[0][1]["is_carousel_item"] == "true"
    assert calls[2][1]["children"] == "container-1,container-2"
    assert calls[3][0].endswith("/threads_publish")


def test_publish_cards_releases_safe_error_before_publish(monkeypatch):
    monkeypatch.setattr(threads_publish, "_threads_credentials", lambda context: ("token", "user"))

    class Proxy:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(threads_publish, "_meta_proxy", lambda: Proxy())
    monkeypatch.setattr(
        threads_publish,
        "_meta_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(threads_publish.ThreadsPublishError("HTTP 400")),
    )

    with pytest.raises(threads_publish.ThreadsPublishError, match="HTTP 400"):
        threads_publish.publish_cards(["https://a/1.png"], "caption", None)


def test_meta_transport_error_is_uncertain(monkeypatch):
    class Response:
        pass

    monkeypatch.setattr(
        threads_publish.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()),
    )

    with pytest.raises(threads_publish.ThreadsDeliveryUncertainError, match="outcome is unknown"):
        threads_publish._meta_post("https://graph.threads.net/v1.0/user/threads", {}, None)


def test_missing_publish_acknowledgement_is_uncertain():
    with pytest.raises(threads_publish.ThreadsDeliveryUncertainError, match="outcome is unknown"):
        threads_publish._published_post_id({})


def test_caption_is_bounded_to_threads_limit():
    value = threads_publish._caption("x" * 501)

    assert len(value) == 500
    assert value.endswith("…")
