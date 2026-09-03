import json

import pytest
import requests

from cs2bot import instagram_publish


def test_upload_public_cards_uses_dedicated_bucket(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MEDIA_BUCKET", "instagram-media")
    seen = []

    class Client:
        def put_object(self, **kwargs):
            seen.append(kwargs)

    monkeypatch.setattr(instagram_publish, "_media_client", lambda: Client())

    urls = instagram_publish.upload_public_cards("schedule_2026-08-30", [b"one", b"two"])

    assert urls == [
        "https://storage.yandexcloud.net/instagram-media/instagram/schedule_2026-08-30/1.png",
        "https://storage.yandexcloud.net/instagram-media/instagram/schedule_2026-08-30/2.png",
    ]
    assert [entry["Bucket"] for entry in seen] == ["instagram-media", "instagram-media"]
    assert all(entry["ACL"] == "public-read" for entry in seen)
    assert all(entry["ContentType"] == "image/png" for entry in seen)


def test_publish_cards_creates_carousel_then_publishes(monkeypatch):
    monkeypatch.setattr(instagram_publish, "_instagram_credentials", lambda context: ("token", "user"))

    class Proxy:
        def __enter__(self):
            return {"https": "http://127.0.0.1:1000"}

        def __exit__(self, *args):
            return False

    calls = []

    def fake_post(url, data, proxy):
        calls.append((url, data, proxy))
        if url.endswith("/media_publish"):
            return {"id": "published-id"}
        return {"id": f"container-{len(calls)}"}

    monkeypatch.setattr(instagram_publish, "_meta_proxy", lambda: Proxy())
    monkeypatch.setattr(instagram_publish, "_meta_post", fake_post)

    assert instagram_publish.publish_cards(["https://a/1.png", "https://a/2.png"], "caption", None) == "published-id"
    assert calls[0][1]["is_carousel_item"] == "true"
    assert calls[1][1]["is_carousel_item"] == "true"
    assert calls[2][1]["media_type"] == "CAROUSEL"
    assert calls[2][1]["children"] == "container-1,container-2"
    assert calls[3][0].endswith("/media_publish")
    assert "token" not in json.dumps([call[0] for call in calls])


def test_publish_cards_keeps_ambiguous_network_outcome_distinct(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"id": "unused"}

    monkeypatch.setattr(
        instagram_publish.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(instagram_publish.requests.ConnectionError("no")),
    )

    with pytest.raises(instagram_publish.InstagramDeliveryUncertainError):
        instagram_publish._meta_post("https://graph.instagram.com/test", {}, None)


@pytest.mark.parametrize("status_code", [500, 503])
def test_meta_server_error_is_uncertain(monkeypatch, status_code):
    class Response:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {"id": "unused"}

    monkeypatch.setattr(instagram_publish.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(instagram_publish.InstagramDeliveryUncertainError):
        instagram_publish._meta_post("https://graph.instagram.com/test", {}, None)


@pytest.mark.parametrize("payload", [[], "invalid"])
def test_meta_malformed_success_data_is_uncertain(monkeypatch, payload):
    class Response:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(instagram_publish.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(instagram_publish.InstagramDeliveryUncertainError):
        instagram_publish._meta_post("https://graph.instagram.com/test", {}, None)


def test_meta_invalid_success_json_is_uncertain(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            raise requests.JSONDecodeError("invalid", "x", 0)

    monkeypatch.setattr(instagram_publish.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(instagram_publish.InstagramDeliveryUncertainError):
        instagram_publish._meta_post("https://graph.instagram.com/test", {}, None)


def test_missing_publish_acknowledgement_is_uncertain():
    with pytest.raises(instagram_publish.InstagramDeliveryUncertainError):
        instagram_publish._published_media_id({})


def test_publication_key_rejects_path_traversal(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MEDIA_BUCKET", "instagram-media")
    with pytest.raises(instagram_publish.InstagramPublishError, match="key is invalid"):
        instagram_publish.upload_public_cards("../private", [b"image"])
