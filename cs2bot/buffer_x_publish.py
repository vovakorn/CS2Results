"""Buffer GraphQL operations for X image posts and delivery recovery."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from .x_content import X_TEXT_LIMIT


BUFFER_GRAPHQL_URL = "https://api.buffer.com"
HTTP_TIMEOUT_SECONDS = 20
MAX_X_IMAGES = 4
MAX_RECENT_POSTS = 100


CREATE_X_POST_MUTATION = """
mutation CreateXPost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        text
        channelId
        status
        createdAt
        dueAt
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""

GET_POST_QUERY = """
query GetBufferPost($input: PostInput!) {
  post(input: $input) {
    id
    text
    channelId
    status
    externalLink
    createdAt
    dueAt
    assets {
      ... on ImageAsset {
        id
        mimeType
        source
      }
    }
  }
}
"""

FIND_RECENT_POSTS_QUERY = """
query FindRecentChannelPosts($first: Int!, $after: String, $input: PostsInput!) {
  posts(first: $first, after: $after, input: $input) {
    edges {
      node {
        id
        text
        channelId
        status
        externalLink
        createdAt
        dueAt
        assets {
          ... on ImageAsset {
            id
            mimeType
            source
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


class BufferXPublishError(RuntimeError):
    """Base error for Buffer X publishing and recovery operations."""


class BufferXConfigurationError(BufferXPublishError):
    """Required Buffer credentials or identifiers are missing or invalid."""


class BufferXApiError(BufferXPublishError):
    """Buffer rejected an HTTP request or returned an API error."""


class BufferXGraphQLError(BufferXApiError):
    """The GraphQL response contains top-level errors."""


class BufferXMutationError(BufferXApiError):
    """The createPost mutation returned its typed MutationError result."""


class BufferXPostNotFoundError(BufferXApiError):
    """Buffer returned no post for the requested ID."""


class BufferXResponseError(BufferXApiError):
    """Buffer returned an unreadable or unexpected response body."""


class BufferXTransportError(BufferXApiError):
    """A read-only Buffer request failed before a response was available."""


class BufferXDeliveryUncertainError(BufferXPublishError):
    """A createPost may have succeeded; automatic retry is unsafe."""


class RecentPosts(list[dict[str, Any]]):
    """Recent post page results, including whether the bounded search truncated."""

    def __init__(self):
        super().__init__()
        self.truncated = False


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BufferXConfigurationError(f"{name} is required")
    return value.strip()


def _validate_images(image_urls: Sequence[str]) -> list[str]:
    if not image_urls or len(image_urls) > MAX_X_IMAGES:
        raise BufferXConfigurationError(
            f"X post must contain between one and {MAX_X_IMAGES} images"
        )
    urls: list[str] = []
    for url in image_urls:
        if not isinstance(url, str):
            raise BufferXConfigurationError("image URL is invalid")
        normalized = url.strip()
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise BufferXConfigurationError("image URL must be public HTTPS")
        urls.append(normalized)
    return urls


def _created_after(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BufferXConfigurationError("created_after must be an ISO datetime") from exc
    else:
        raise BufferXConfigurationError("created_after must be an ISO datetime")
    if parsed.tzinfo is None:
        raise BufferXConfigurationError("created_after must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _response_json(response: Any, *, is_create: bool) -> dict[str, Any]:
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        raise BufferXResponseError("Buffer returned an invalid HTTP response")
    if status_code >= 500:
        if is_create:
            raise BufferXDeliveryUncertainError(
                f"Buffer returned HTTP {status_code}; post outcome is unknown"
            )
        raise BufferXApiError(f"Buffer returned HTTP {status_code}")
    if status_code < 200 or status_code >= 300:
        raise BufferXApiError(f"Buffer returned HTTP {status_code}")
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        if is_create:
            raise BufferXDeliveryUncertainError(
                "Buffer response could not confirm whether the post was created"
            ) from exc
        raise BufferXResponseError("Buffer returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BufferXResponseError("Buffer returned an invalid GraphQL response")
    errors = payload.get("errors")
    if errors:
        messages = [
            item.get("message", "GraphQL error")
            for item in errors
            if isinstance(item, dict)
        ]
        message = "; ".join(str(item) for item in messages) or "GraphQL error"
        raise BufferXGraphQLError(message[:500])
    return payload


def _execute(
    api_key: str,
    query: str,
    variables: dict[str, Any],
    *,
    http_client: Any,
    is_create: bool = False,
) -> dict[str, Any]:
    token = _required(api_key, "api_key")
    try:
        response = http_client.post(
            BUFFER_GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        if is_create:
            raise BufferXDeliveryUncertainError(
                "Buffer createPost outcome is unknown; do not retry automatically"
            ) from exc
        raise BufferXTransportError("Buffer read request failed") from exc
    return _response_json(response, is_create=is_create)


def create_x_post(
    api_key: str,
    channel_id: str,
    text: str,
    image_urls: Sequence[str],
    *,
    http_client: Any = requests,
) -> dict[str, Any]:
    """Queue an X image post in Buffer without retrying an ambiguous create."""
    channel = _required(channel_id, "channel_id")
    if not isinstance(text, str) or not text.strip():
        raise BufferXConfigurationError("post text is required")
    normalized_text = text.strip()
    if len(normalized_text) > X_TEXT_LIMIT:
        raise BufferXConfigurationError(f"post text exceeds {X_TEXT_LIMIT} characters")
    urls = _validate_images(image_urls)
    payload = _execute(
        api_key,
        CREATE_X_POST_MUTATION,
        {
            "input": {
                "text": normalized_text,
                "channelId": channel,
                "schedulingType": "automatic",
                "mode": "addToQueue",
                "assets": [{"image": {"url": url}} for url in urls],
            }
        },
        http_client=http_client,
        is_create=True,
    )
    data = payload.get("data")
    action = data.get("createPost") if isinstance(data, dict) else None
    if not isinstance(action, dict):
        raise BufferXDeliveryUncertainError(
            "Buffer createPost response did not confirm the post"
        )
    if action.get("__typename") == "MutationError" or action.get("message"):
        message = action.get("message")
        raise BufferXMutationError(str(message or "Buffer rejected createPost")[:500])
    post = action.get("post")
    if action.get("__typename") != "PostActionSuccess" or not isinstance(post, dict):
        raise BufferXDeliveryUncertainError(
            "Buffer createPost response did not confirm the post"
        )
    if not isinstance(post.get("id"), str) or not post["id"]:
        raise BufferXDeliveryUncertainError(
            "Buffer createPost response did not include a post ID"
        )
    return post


def get_post(
    api_key: str,
    post_id: str,
    *,
    http_client: Any = requests,
) -> dict[str, Any]:
    """Fetch a single Buffer post by its Buffer ID."""
    identifier = _required(post_id, "post_id")
    payload = _execute(
        api_key,
        GET_POST_QUERY,
        {"input": {"id": identifier}},
        http_client=http_client,
    )
    data = payload.get("data")
    post = data.get("post") if isinstance(data, dict) else None
    if post is None:
        raise BufferXPostNotFoundError("Buffer post was not found")
    if not isinstance(post, dict):
        raise BufferXResponseError("Buffer returned an invalid post")
    return post


def find_recent_channel_posts(
    api_key: str,
    organization_id: str,
    channel_id: str,
    created_after: datetime | str,
    *,
    created_before: datetime | str | None = None,
    first: int = 50,
    max_pages: int | None = None,
    http_client: Any = requests,
) -> list[dict[str, Any]]:
    """Search newest posts on one channel since a recovery timestamp."""
    organization = _required(organization_id, "organization_id")
    channel = _required(channel_id, "channel_id")
    if isinstance(first, bool) or not isinstance(first, int) or not 1 <= first <= MAX_RECENT_POSTS:
        raise BufferXConfigurationError(
            f"first must be between 1 and {MAX_RECENT_POSTS}"
        )
    if max_pages is not None and (
        isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1
    ):
        raise BufferXConfigurationError("max_pages must be a positive integer")
    posts = RecentPosts()
    after: str | None = None
    created_at = _created_after(created_after)
    created_until = _created_after(created_before) if created_before is not None else None
    if created_until is not None and created_until < created_at:
        raise BufferXConfigurationError("created_before must be at or after created_after")
    page_count = 0
    created_at_filter = {"start": created_at}
    if created_until is not None:
        created_at_filter["end"] = created_until
    while True:
        payload = _execute(
            api_key,
            FIND_RECENT_POSTS_QUERY,
            {
                "first": first,
                "after": after,
                "input": {
                    "organizationId": organization,
                    "filter": {
                        "channelIds": [channel],
                        "createdAt": created_at_filter,
                    },
                    "sort": [{"field": "createdAt", "direction": "desc"}],
                },
            },
            http_client=http_client,
        )
        page_count += 1
        data = payload.get("data")
        connection = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(connection, dict):
            raise BufferXResponseError("Buffer returned an invalid posts connection")
        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise BufferXResponseError("Buffer returned invalid post edges")
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict) and node.get("channelId") == channel:
                posts.append(node)
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise BufferXResponseError("Buffer returned invalid page information")
        if not page_info.get("hasNextPage"):
            break
        if max_pages is not None and page_count >= max_pages:
            posts.truncated = True
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor == after:
            raise BufferXResponseError("Buffer returned an invalid pagination cursor")
        after = cursor
    return posts
