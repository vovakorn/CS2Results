"""Bounded retries of daily digest preparation, never of ambiguous delivery."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError

from .match_sources import storage

PREFIX = "outbox/digest-preparation/"
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=5)
LEASE = timedelta(minutes=10)
MAX_AGE = timedelta(hours=2)
MOSCOW = ZoneInfo("Europe/Moscow")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("digest timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("digest timestamp must be timezone aware")
    return parsed


@dataclass(frozen=True)
class DigestAttempt:
    payload: dict[str, Any]
    etag: str

    @property
    def day_key(self) -> str:
        return self.payload["day_key"]

    @property
    def key(self) -> str:
        return f"{PREFIX}{self.day_key}.json"


class DigestRetryQueue:
    def __init__(self, client: Any = None, bucket: str | None = None):
        self.client = client if client is not None else storage._client()
        self.bucket = bucket or storage._bucket()

    def _load(self, day_key: str) -> DigestAttempt | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=f"{PREFIX}{day_key}.json")
            stream = response["Body"]
            try:
                payload = json.loads(stream.read())
            finally:
                stream.close()
            if (type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["day_key"] != day_key
                    or payload["status"] not in {"pending", "attempting", "prepared", "exhausted"}
                    or type(payload["attempts"]) is not int or not 1 <= payload["attempts"] <= MAX_ATTEMPTS
                    or not isinstance(payload["generations"], dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0
                           for k, v in payload["generations"].items())):
                raise ValueError("invalid digest retry state")
            for name in ("window_start", "window_end", "display_at", "created_at", "next_attempt_at"):
                _time(payload[name])
            if payload["status"] == "attempting":
                _time(payload["lease_until"])
            local_start = _time(payload["window_start"]).astimezone(MOSCOW)
            if (_time(payload["window_end"]) - _time(payload["window_start"]) != timedelta(days=1)
                    or local_start.date().isoformat() != day_key
                    or local_start.time().isoformat() != "00:00:00"
                    or _time(payload["display_at"]).astimezone(MOSCOW).date().isoformat() != day_key
                    or not response.get("ETag")):
                raise ValueError("invalid digest retry window")
            return DigestAttempt(payload, response["ETag"])
        except ClientError as exc:
            if storage._is_not_found(exc):
                return None
            raise storage.StorageUnavailableError("digest retry read failed") from exc
        except (BotoCoreError, KeyError, TypeError, ValueError) as exc:
            raise storage.StorageUnavailableError("digest retry state unavailable") from exc

    def _write(self, payload: dict[str, Any], previous: DigestAttempt | None = None) -> DigestAttempt | None:
        try:
            result = self.client.put_object(
                Bucket=self.bucket, Key=f"{PREFIX}{payload['day_key']}.json",
                Body=json.dumps(payload, sort_keys=True).encode(), ContentType="application/json",
                **({"IfMatch": previous.etag} if previous else {"IfNoneMatch": "*"}),
            )
            if not result.get("ETag"):
                raise ValueError("digest retry write has no ETag")
            return DigestAttempt(payload, result["ETag"])
        except ClientError as exc:
            if storage._is_precondition_failed(exc):
                return None
            raise storage.StorageUnavailableError("digest retry write failed") from exc
        except (BotoCoreError, ValueError) as exc:
            raise storage.StorageUnavailableError("digest retry write unavailable") from exc

    def enqueue_failure(self, *, start: datetime, end: datetime, display_at: datetime,
                        generations: dict[str, int], now: datetime | None = None) -> DigestAttempt | None:
        """Record the first failure without replacing an existing edition or its generations."""
        now = now or datetime.now(timezone.utc)
        return self._write({
            "schema_version": 1, "day_key": display_at.astimezone(MOSCOW).date().isoformat(),
            "window_start": _iso(start), "window_end": _iso(end), "display_at": display_at.isoformat(),
            "generations": dict(generations), "created_at": _iso(now), "attempts": 1,
            "status": "pending", "next_attempt_at": _iso(now + RETRY_DELAY),
            "reason": "source_unavailable",
        })

    def claim_due(self, now: datetime | None = None) -> DigestAttempt | None:
        """Check two date-addressed objects and lease at most one edition per worker invocation."""
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(MOSCOW).date()
        for day in (today - timedelta(days=1), today):
            previous = self._load(day.isoformat())
            if previous is None or previous.payload["status"] in {"prepared", "exhausted"}:
                continue
            p = previous.payload
            if p["status"] == "attempting" and _time(p["lease_until"]) > now:
                continue
            if _time(p["next_attempt_at"]) > now:
                continue
            if p["attempts"] >= MAX_ATTEMPTS or now >= _time(p["created_at"]) + MAX_AGE:
                # A crashed third attempt or expired edition must not become an unbounded retry.
                expired = self._write({**p, "status": "exhausted", "reason": "preparation_expired"}, previous)
                if expired:
                    return expired
                continue
            claimed = self._write({**p, "status": "attempting", "attempts": p["attempts"] + 1,
                                   "lease_until": _iso(now + LEASE)}, previous)
            if claimed:
                return claimed
        return None

    def finish(self, attempt: DigestAttempt, *, prepared: bool,
               now: datetime | None = None) -> DigestAttempt | None:
        now = now or datetime.now(timezone.utc)
        if attempt.payload["status"] != "attempting":
            raise ValueError("digest retry is not leased")
        return self._write({
            **attempt.payload,
            "status": "prepared" if prepared else (
                "exhausted" if attempt.payload["attempts"] >= MAX_ATTEMPTS else "pending"),
            "reason": None if prepared else "source_unavailable",
            "next_attempt_at": _iso(now + RETRY_DELAY),
            "updated_at": _iso(now),
        }, attempt)
