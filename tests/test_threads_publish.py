import hashlib

import pytest
import requests

from cs2bot import threads_publish


@pytest.fixture(autouse=True)
def ready_containers(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"status": "FINISHED"}
    monkeypatch.setattr(threads_publish.requests, "get", lambda *args, **kwargs: Response())


def test_upload_public_cards_persists_threads_urls(monkeypatch):
    monkeypatch.setenv("THREADS_MEDIA_BUCKET", "social-media")
    uploaded = []

    class Client:
        def put_object(self, **kwargs):
            uploaded.append(kwargs)

    monkeypatch.setattr(threads_publish, "_media_client", lambda: Client())

    assert threads_publish.upload_public_cards("schedule_2026-08-30", [b"one", b"two"]) == [
        f"https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/1-{hashlib.sha256(b'one').hexdigest()}.png",
        f"https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/2-{hashlib.sha256(b'two').hexdigest()}.png",
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


def test_publish_image_reply_sets_reply_to_id(monkeypatch):
    monkeypatch.setattr(threads_publish, "_threads_credentials", lambda context: ("token", "user"))
    class Proxy:
        def __enter__(self): return None
        def __exit__(self, *args): return False
    calls = []
    def fake_post(url, data, proxy):
        calls.append((url, data))
        return {"id": "post-id" if url.endswith("/threads_publish") else "container-id"}
    monkeypatch.setattr(threads_publish, "_meta_proxy", lambda: Proxy())
    monkeypatch.setattr(threads_publish, "_meta_post", fake_post)
    assert threads_publish.publish_cards(["https://a/1.png"], "caption", None, "parent-id") == "post-id"
    assert calls[0][1]["reply_to_id"] == "parent-id"


def test_publish_carousel_reply_sets_parent_reply_to_id(monkeypatch):
    monkeypatch.setattr(threads_publish, "_threads_credentials", lambda context: ("token", "user"))
    class Proxy:
        def __enter__(self): return None
        def __exit__(self, *args): return False
    calls = []
    def fake_post(url, data, proxy):
        calls.append((url, data))
        return {"id": f"id-{len(calls)}"}
    monkeypatch.setattr(threads_publish, "_meta_proxy", lambda: Proxy())
    monkeypatch.setattr(threads_publish, "_meta_post", fake_post)
    threads_publish.publish_cards(["https://a/1.png", "https://a/2.png"], "caption", None, "parent-id")
    assert calls[2][1]["media_type"] == "CAROUSEL"
    assert calls[2][1]["reply_to_id"] == "parent-id"
    assert "reply_to_id" not in calls[0][1]


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


@pytest.fixture
def publishing(monkeypatch):
    from contextlib import nullcontext
    clock = [0.0]
    calls = []
    monkeypatch.setattr(threads_publish, "_threads_credentials", lambda context: ("private-token", "user"))
    monkeypatch.setattr(threads_publish, "_meta_proxy", lambda: nullcontext({"https": "proxy"}))
    monkeypatch.setattr(threads_publish.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(threads_publish.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    def post(url, data, proxy):
        calls.append((data.get("media_type", "publish"), data))
        return {"id": f"id-{len(calls)}"}
    monkeypatch.setattr(threads_publish, "_meta_post", post)
    return calls, clock


def test_media_waits_until_finished_then_publishes_once(monkeypatch, publishing):
    calls, clock = publishing
    statuses = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    def get(url, **kwargs):
        assert kwargs["proxies"] == {"https": "proxy"}
        assert kwargs["headers"]["Authorization"] == "Bearer private-token"
        assert kwargs["params"] == {"fields": "status"}
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"status": next(statuses)}
        return Response()
    monkeypatch.setattr(threads_publish.requests, "get", get)
    assert threads_publish.publish_cards(["https://a/1.png"], "caption", None) == "id-2"
    assert clock[0] == 4
    assert [item[0] for item in calls] == ["IMAGE", "publish"]


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED", "UNKNOWN", None])
def test_unusable_media_never_publishes(monkeypatch, publishing, status):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"status": status, "error_message": "private-token"}
    monkeypatch.setattr(threads_publish.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(threads_publish.ThreadsPublishError) as error:
        threads_publish.publish_cards(["https://a/1.png"], "caption", None)
    assert not isinstance(error.value, threads_publish.ThreadsDeliveryUncertainError)
    assert "private-token" not in str(error.value)
    assert [item[0] for item in publishing[0]] == ["IMAGE"]


def test_shared_readiness_budget_prevents_publishing_unready_carousel(monkeypatch, publishing):
    calls, clock = publishing
    def get(url, **kwargs):
        identifier = url.rsplit("/", 1)[-1]
        class Response:
            def raise_for_status(self): pass
            def json(self):
                if identifier == "id-1":
                    return {"status": "FINISHED" if clock[0] >= 28 else "IN_PROGRESS"}
                if identifier == "id-2":
                    return {"status": "FINISHED"}
                return {"status": "IN_PROGRESS"}
        return Response()
    monkeypatch.setattr(threads_publish.requests, "get", get)
    with pytest.raises(threads_publish.ThreadsPublishError, match="timed out"):
        threads_publish.publish_cards(["https://a/1.png", "https://a/2.png"], "caption", None)
    assert clock[0] == 30
    assert [item[0] for item in calls] == ["IMAGE", "IMAGE", "CAROUSEL"]


def test_status_transport_error_is_retryable_without_publication(monkeypatch, publishing):
    def fail(*args, **kwargs): raise requests.Timeout("private-token")
    monkeypatch.setattr(threads_publish.requests, "get", fail)
    with pytest.raises(threads_publish.ThreadsPublishError) as error:
        threads_publish.publish_cards(["https://a/1.png"], "caption", None)
    assert not isinstance(error.value, threads_publish.ThreadsDeliveryUncertainError)
    assert "private-token" not in str(error.value)
    assert len(publishing[0]) == 1


def test_already_published_container_blocks_automatic_retry(monkeypatch, publishing):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"status": "PUBLISHED"}
    monkeypatch.setattr(threads_publish.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(threads_publish.ThreadsDeliveryUncertainError):
        threads_publish.publish_cards(["https://a/1.png"], "caption", None)
    assert len(publishing[0]) == 1
