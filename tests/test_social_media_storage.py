import pytest

from cs2bot import social_media_storage, threads_publish


class FakeS3Client:
    def __init__(self, error=None):
        self.uploads = []
        self.error = error

    def put_object(self, **kwargs):
        if self.error:
            raise self.error
        self.uploads.append(kwargs)


def test_x_uploads_one_png_under_stable_prefix():
    client = FakeS3Client()

    urls = social_media_storage.upload_public_pngs(
        "x",
        "schedule_2026-09-29",
        [b"png-one"],
        bucket="social-media",
        base_url="https://cdn.example",
        client=client,
    )

    assert urls == ["https://cdn.example/x/schedule_2026-09-29/1.png"]
    assert [item["Key"] for item in client.uploads] == [
        "x/schedule_2026-09-29/1.png"
    ]
    assert client.uploads[0]["Body"] == b"png-one"
    assert client.uploads[0]["ContentType"] == "image/png"
    assert client.uploads[0]["ACL"] == "public-read"


def test_x_uploads_four_pngs_without_trimming_album():
    client = FakeS3Client()

    urls = social_media_storage.upload_public_pngs(
        "x",
        "digest_2026-09-29",
        [b"png-1", b"png-2", b"png-3", b"png-4"],
        bucket="social-media",
        base_url="https://cdn.example/",
        client=client,
    )

    assert len(urls) == len(client.uploads) == 4
    assert [item["Key"] for item in client.uploads] == [
        "x/digest_2026-09-29/1.png",
        "x/digest_2026-09-29/2.png",
        "x/digest_2026-09-29/3.png",
        "x/digest_2026-09-29/4.png",
    ]
    assert urls[-1] == "https://cdn.example/x/digest_2026-09-29/4.png"


def test_invalid_publication_key_is_rejected_before_s3_upload():
    client = FakeS3Client()

    with pytest.raises(social_media_storage.PublicMediaUploadError) as exc_info:
        social_media_storage.upload_public_pngs(
            "x",
            "schedule/2026-09-29",
            [b"png"],
            bucket="social-media",
            base_url="https://cdn.example",
            client=client,
        )

    assert exc_info.value.reason == "invalid_key"
    assert client.uploads == []


def test_empty_png_is_rejected_before_s3_upload():
    client = FakeS3Client()

    with pytest.raises(social_media_storage.PublicMediaUploadError) as exc_info:
        social_media_storage.upload_public_pngs(
            "x",
            "schedule_2026-09-29",
            [b""],
            bucket="social-media",
            base_url="https://cdn.example",
            client=client,
        )

    assert exc_info.value.reason == "invalid_image"
    assert client.uploads == []


def test_s3_error_is_reported_without_exposing_underlying_message():
    client = FakeS3Client(RuntimeError("private storage detail"))

    with pytest.raises(social_media_storage.PublicMediaUploadError) as exc_info:
        social_media_storage.upload_public_pngs(
            "x",
            "result_2026-09-29",
            [b"png"],
            bucket="social-media",
            base_url="https://cdn.example",
            client=client,
        )

    assert exc_info.value.reason == "upload_failed"
    assert "private storage detail" not in str(exc_info.value)


def test_threads_upload_keeps_legacy_keys_and_urls(monkeypatch):
    monkeypatch.setenv("THREADS_MEDIA_BUCKET", "social-media")
    client = FakeS3Client()
    monkeypatch.setattr(threads_publish, "_media_client", lambda: client)

    urls = threads_publish.upload_public_cards(
        "schedule_2026-08-30", [b"png-one", b"png-two"]
    )

    assert urls == [
        "https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/1.png",
        "https://storage.yandexcloud.net/social-media/threads/schedule_2026-08-30/2.png",
    ]
    assert [item["Key"] for item in client.uploads] == [
        "threads/schedule_2026-08-30/1.png",
        "threads/schedule_2026-08-30/2.png",
    ]
