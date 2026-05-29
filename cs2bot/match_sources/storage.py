from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .config import OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_ENDPOINT
from .models import MatchNormalized

logger = logging.getLogger(__name__)


class StorageUnavailableError(Exception):
    """Raised when Object Storage is not configured or cannot be reached."""


def _client():
    return boto3.client("s3", endpoint_url=OBJECT_STORAGE_ENDPOINT)


def _bucket() -> str:
    if not OBJECT_STORAGE_BUCKET:
        raise StorageUnavailableError("OBJECT_STORAGE_BUCKET is not configured")
    return OBJECT_STORAGE_BUCKET


def processed_key(match_uid: str) -> str:
    return f"processed/{match_uid}.json"


def safe_storage_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "unknown"


def channel_match_uid(match: MatchNormalized, channel_name: str) -> str:
    return f"{safe_storage_part(channel_name)}_{match.match_uid}"


async def is_processed(match_uid: str, client: Any | None = None, bucket: str | None = None) -> bool:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = processed_key(match_uid)

    def _head() -> bool:
        try:
            s3.head_object(Bucket=bucket_name, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise

    try:
        return await asyncio.to_thread(_head)
    except ClientError as exc:
        logger.error('storage_error="head_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"head_object failed for {key}") from exc


async def mark_processed(match: MatchNormalized, client: Any | None = None, bucket: str | None = None) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    key = processed_key(match.match_uid)
    payload = {
        "match_uid": match.match_uid,
        "source": match.source,
        "match_id": match.match_id,
        "tournament_name": match.tournament_name,
        "team1_name": match.team1_name,
        "team2_name": match.team2_name,
        "score1": match.score1,
        "score2": match.score2,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        logger.error('storage_error="put_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"put_object failed for {key}") from exc


async def mark_channel_processed(
    match: MatchNormalized,
    channel_name: str,
    client: Any | None = None,
    bucket: str | None = None,
) -> None:
    s3 = client or _client()
    bucket_name = bucket or _bucket()
    match_uid = channel_match_uid(match, channel_name)
    key = processed_key(match_uid)
    payload = {
        "match_uid": match_uid,
        "source": match.source,
        "match_id": match.match_id,
        "channel_name": channel_name,
        "tournament_name": match.tournament_name,
        "team1_name": match.team1_name,
        "team2_name": match.team2_name,
        "score1": match.score1,
        "score2": match.score2,
        "start_date": match.start_date,
        "end_date": match.end_date,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def _put() -> None:
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
    except ClientError as exc:
        logger.error('storage_error="put_object failed" key="%s" error="%s"', key, exc)
        raise StorageUnavailableError(f"put_object failed for {key}") from exc
