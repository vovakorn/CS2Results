"""Durable first observations for the bounded final-card preparation window."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from . import storage
from .models import MatchNormalized

PREFIX = "pending/final-format/"
OBSERVATIONS_PREFIX = "observations/final-format/"


class FinalWait(BaseModel):
    match: MatchNormalized
    first_seen_at: datetime


async def _read(s3, bucket: str, key: str) -> FinalWait | None:
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=bucket, Key=key)
        stream = response["Body"]
        try:
            value = FinalWait.model_validate_json(stream.read())
        finally:
            stream.close()
        if value.first_seen_at.tzinfo is None or value.match.source != "pandascore":
            raise ValueError("invalid final wait observation")
        return value
    except ClientError as exc:
        if storage._is_not_found(exc):
            return None
        raise storage.StorageUnavailableError("final wait read failed") from exc
    except BotoCoreError as exc:
        raise storage.StorageUnavailableError("final wait read failed") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise storage.StorageUnavailableError("invalid final wait observation") from exc


async def observe_primary(match: MatchNormalized, *, now: datetime,
                          dry_run: bool = False, client=None, bucket=None) -> FinalWait:
    """Conditional create preserves the first observation across concurrent runs."""
    s3, bucket_name = client or storage._client(), bucket or storage._bucket()
    key = f"{PREFIX}{storage.safe_storage_part(match.match_uid)}.json"
    existing = await _read(s3, bucket_name, key)
    if existing:
        return existing
    observation = FinalWait(match=match, first_seen_at=now)
    if dry_run:
        return observation
    if match.source != "pandascore":
        raise ValueError("final wait requires a PandaScore result")
    try:
        await asyncio.to_thread(s3.put_object, Bucket=bucket_name, Key=key,
                               Body=observation.model_dump_json().encode(),
                               ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if not storage._is_precondition_failed(exc):
            raise storage.StorageUnavailableError("final wait write failed") from exc
    except BotoCoreError as exc:
        raise storage.StorageUnavailableError("final wait write failed") from exc
    saved = await _read(s3, bucket_name, key)
    if saved is None:
        raise storage.StorageUnavailableError("final wait observation disappeared")
    return saved


async def list_waits(*, now: datetime, max_age_hours: int, dry_run: bool = False,
                     client=None, bucket=None) -> list[FinalWait]:
    """Replay held results even after they leave the provider's latest-N page."""
    s3, bucket_name = client or storage._client(), bucket or storage._bucket()
    values = []
    token = None
    seen_tokens = set()
    while True:
        kwargs = dict(Bucket=bucket_name, Prefix=PREFIX, MaxKeys=1000)
        if token:
            kwargs["ContinuationToken"] = token
        try:
            page = await asyncio.to_thread(s3.list_objects_v2, **kwargs)
            for item in page.get("Contents", []):
                key = item["Key"]
                value = await _read(s3, bucket_name, key)
                if value is None:
                    continue
                # Retain the observation longer than the source freshness window.
                # Completed measurements live in the separate observations prefix.
                if now - value.first_seen_at > timedelta(hours=max_age_hours + 24):
                    if not dry_run:
                        await asyncio.to_thread(s3.delete_object, Bucket=bucket_name, Key=key)
                    continue
                values.append(value)
        except (ClientError, BotoCoreError) as exc:
            raise storage.StorageUnavailableError("final wait list failed") from exc
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token or token in seen_tokens:
            raise storage.StorageUnavailableError("invalid final wait pagination")
        seen_tokens.add(token)
    return sorted(values, key=lambda value: value.first_seen_at)


async def observe_resolution(wait: FinalWait, *, now: datetime, outcome: str,
                             ready: bool, client=None, bucket=None) -> None:
    """Store the first ready sample and first timeout independently, without secrets."""
    s3, bucket_name = client or storage._client(), bucket or storage._bucket()
    phase = "liquipedia_ready" if ready else "timeout"
    key = f"{OBSERVATIONS_PREFIX}{storage.safe_storage_part(wait.match.match_uid)}/{phase}.json"
    payload = {"match_uid": wait.match.match_uid, "match_id": wait.match.match_id,
               "tournament": wait.match.tournament_name, "outcome": outcome,
               "pandascore_first_seen_at": wait.first_seen_at.isoformat(),
               "observed_at": now.isoformat(),
               "observed_delay_seconds": max(0, (now - wait.first_seen_at).total_seconds()),
               "match_end_at": wait.match.end_date}
    try:
        await asyncio.to_thread(s3.put_object, Bucket=bucket_name, Key=key,
                               Body=json.dumps(payload, ensure_ascii=False).encode(),
                               ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if not storage._is_precondition_failed(exc):
            raise storage.StorageUnavailableError("final resolution observation failed") from exc
    except BotoCoreError as exc:
        raise storage.StorageUnavailableError("final resolution observation failed") from exc
