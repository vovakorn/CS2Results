from datetime import datetime, timezone

import pytest
import requests

from cs2bot import buffer_x_publish as buffer_x


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeHTTPClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _post_action(post_id="post-123"):
    return {
        "data": {
            "createPost": {
                "__typename": "PostActionSuccess",
                "post": {
                    "id": post_id,
                    "text": "CS2 · 2026-09-29\nNAVI 2:1 FaZe",
                    "channelId": "channel-x",
                    "status": "scheduled",
                    "createdAt": "2026-09-29T10:00:00Z",
                    "dueAt": "2026-09-29T12:00:00Z",
                },
            }
        }
    }


def test_create_x_post_sends_text_and_public_image_urls():
    client = FakeHTTPClient(FakeResponse(_post_action()))

    post = buffer_x.create_x_post(
        "test-key",
        "channel-x",
        "CS2 · 2026-09-29\nNAVI 2:1 FaZe",
        ["https://cdn.example/x/results/1.png", "https://cdn.example/x/results/2.png"],
        http_client=client,
    )

    assert post["id"] == "post-123"
    url, request = client.calls[0]
    assert url == buffer_x.BUFFER_GRAPHQL_URL
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["timeout"] == buffer_x.HTTP_TIMEOUT_SECONDS
    variables = request["json"]["variables"]["input"]
    assert variables == {
        "text": "CS2 · 2026-09-29\nNAVI 2:1 FaZe",
        "channelId": "channel-x",
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [
            {"image": {"url": "https://cdn.example/x/results/1.png"}},
            {"image": {"url": "https://cdn.example/x/results/2.png"}},
        ],
    }
    assert "... on MutationError" in request["json"]["query"]


def test_create_mutation_error_in_http_200_is_raised():
    client = FakeHTTPClient(
        FakeResponse(
            {"data": {"createPost": {"__typename": "MutationError", "message": "Bad asset"}}}
        )
    )

    with pytest.raises(buffer_x.BufferXMutationError, match="Bad asset"):
        buffer_x.create_x_post(
            "test-key", "channel-x", "Hello", ["https://cdn.example/1.png"], http_client=client
        )


def test_top_level_graphql_errors_are_raised():
    client = FakeHTTPClient(FakeResponse({"errors": [{"message": "Not authorized"}]}))

    with pytest.raises(buffer_x.BufferXGraphQLError, match="Not authorized"):
        buffer_x.create_x_post(
            "test-key", "channel-x", "Hello", ["https://cdn.example/1.png"], http_client=client
        )


@pytest.mark.parametrize(
    "response, expected_error",
    [
        (FakeResponse({"errors": [{"message": "Bad request"}]}, status_code=400), buffer_x.BufferXApiError),
        (FakeResponse(ValueError("invalid JSON")), buffer_x.BufferXDeliveryUncertainError),
        (FakeResponse(_post_action("")), buffer_x.BufferXDeliveryUncertainError),
        (FakeResponse({"data": {"createPost": {"__typename": "OtherResult"}}}), buffer_x.BufferXDeliveryUncertainError),
    ],
)
def test_create_handles_http_and_malformed_success_responses(response, expected_error):
    client = FakeHTTPClient(response)

    with pytest.raises(expected_error):
        buffer_x.create_x_post(
            "test-key", "channel-x", "Hello", ["https://cdn.example/1.png"], http_client=client
        )


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("request timed out"),
        requests.ConnectionError("connection dropped"),
    ],
)
def test_create_transport_ambiguity_is_not_retried(failure):
    client = FakeHTTPClient(failure)

    with pytest.raises(buffer_x.BufferXDeliveryUncertainError):
        buffer_x.create_x_post(
            "test-key", "channel-x", "Hello", ["https://cdn.example/1.png"], http_client=client
        )

    assert len(client.calls) == 1
    assert "createPost" in client.calls[0][1]["json"]["query"]


def test_create_http_5xx_is_uncertain_and_not_retried():
    client = FakeHTTPClient(FakeResponse({"message": "server error"}, status_code=503))

    with pytest.raises(buffer_x.BufferXDeliveryUncertainError):
        buffer_x.create_x_post(
            "test-key", "channel-x", "Hello", ["https://cdn.example/1.png"], http_client=client
        )

    assert len(client.calls) == 1


def test_get_post_queries_by_buffer_id():
    post = {"id": "post-123", "channelId": "channel-x", "text": "Hello", "assets": []}
    client = FakeHTTPClient(FakeResponse({"data": {"post": post}}))

    assert buffer_x.get_post("test-key", "post-123", http_client=client) == post
    request = client.calls[0][1]["json"]
    assert request["variables"] == {"input": {"id": "post-123"}}
    assert "post(input: $input)" in request["query"]
    assert "externalLink" in request["query"]


def test_get_post_missing_id_raises_not_found():
    client = FakeHTTPClient(FakeResponse({"data": {"post": None}}))

    with pytest.raises(buffer_x.BufferXPostNotFoundError):
        buffer_x.get_post("test-key", "missing", http_client=client)


def test_get_post_malformed_body_raises_response_error():
    client = FakeHTTPClient(FakeResponse(["unexpected", "json", "shape"]))

    with pytest.raises(buffer_x.BufferXResponseError):
        buffer_x.get_post("test-key", "post-123", http_client=client)


def test_recent_posts_search_filters_channel_and_paginates():
    first_page = FakeResponse(
        {
            "data": {
                "posts": {
                    "edges": [
                        {"node": {"id": "newest", "channelId": "channel-x"}},
                        {"node": {"id": "other", "channelId": "other-channel"}},
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }
            }
        }
    )
    second_page = FakeResponse(
        {
            "data": {
                "posts": {
                    "edges": [{"node": {"id": "older", "channelId": "channel-x"}}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                }
            }
        }
    )
    client = FakeHTTPClient(first_page, second_page)

    posts = buffer_x.find_recent_channel_posts(
        "test-key",
        "org-1",
        "channel-x",
        datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc),
        first=10,
        http_client=client,
    )

    assert [post["id"] for post in posts] == ["newest", "older"]
    first_variables = client.calls[0][1]["json"]["variables"]
    assert first_variables["input"] == {
        "organizationId": "org-1",
        "filter": {
            "channelIds": ["channel-x"],
            "createdAt": {"start": "2026-09-29T09:00:00Z"},
        },
        "sort": [{"field": "createdAt", "direction": "desc"}],
    }
    assert client.calls[1][1]["json"]["variables"]["after"] == "cursor-1"


def test_recent_posts_bounded_page_reports_truncation():
    response = FakeResponse(
        {
            "data": {
                "posts": {
                    "edges": [{"node": {"id": "candidate", "channelId": "channel-x"}}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }
            }
        }
    )
    client = FakeHTTPClient(response)

    posts = buffer_x.find_recent_channel_posts(
        "test-key", "org-1", "channel-x",
        datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc),
        created_before=datetime(2026, 9, 29, 9, 5, tzinfo=timezone.utc),
        max_pages=1, http_client=client,
    )

    assert [post["id"] for post in posts] == ["candidate"]
    assert posts.truncated is True
    assert len(client.calls) == 1
    assert client.calls[0][1]["json"]["variables"]["input"]["filter"]["createdAt"] == {
        "start": "2026-09-29T09:00:00Z",
        "end": "2026-09-29T09:05:00Z",
    }


def test_read_timeout_is_a_transport_error_not_create_uncertainty():
    client = FakeHTTPClient(requests.Timeout("timed out"))

    with pytest.raises(buffer_x.BufferXTransportError):
        buffer_x.get_post("test-key", "post-123", http_client=client)


@pytest.mark.parametrize(
    "api_key, channel_id, text, image_urls",
    [
        ("", "channel-x", "Hello", ["https://cdn.example/1.png"]),
        ("test-key", "", "Hello", ["https://cdn.example/1.png"]),
        ("test-key", "channel-x", "", ["https://cdn.example/1.png"]),
        ("test-key", "channel-x", "x" * 241, ["https://cdn.example/1.png"]),
        ("test-key", "channel-x", "Hello", []),
        ("test-key", "channel-x", "Hello", ["https://cdn.example/1.png"] * 5),
        ("test-key", "channel-x", "Hello", ["http://cdn.example/1.png"]),
    ],
)
def test_invalid_create_input_fails_before_http(api_key, channel_id, text, image_urls):
    client = FakeHTTPClient()

    with pytest.raises(buffer_x.BufferXConfigurationError):
        buffer_x.create_x_post(api_key, channel_id, text, image_urls, http_client=client)

    assert client.calls == []
