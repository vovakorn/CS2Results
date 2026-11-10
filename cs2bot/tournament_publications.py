"""Durable preparation obligations for the three independent tournament outputs.

Delivery stays in the existing outbox/claims protocol. This registry survives
source pagination, late payouts, missing VRS updates and ordinary result posts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

from .match_sources import storage
from .match_sources.match_fetcher import confirm_liquipedia_final_candidates
from .match_sources.filters import is_tier1_event, is_tier1_lan, is_valid_match
from .match_sources.models import MatchNormalized, TournamentVRSImpact
from .match_sources.sources.liquipedia_source import fetch_finished_matches
from .match_sources.sources.tournament_preview_source import fetch_preview_calendar
from .match_sources.vrs import VRSDataError, calculate_impacts
from .tournament_identity import event_for_match
from .tournament_preview import load_profiles
from .vrs_baseline_job import calendar_baseline_id, validate_before

PREFIX = "tournament-publications/events/"
REPORT_KEY = "diagnostics/tournament-publications/latest.json"
DISCOVERY_KEY = "tournament-publications/discovery.json"
MOSCOW = ZoneInfo("Europe/Moscow")
KINDS = ("tournament_final", "tournament_standings", "tournament_vrs_standings")
ALERT_HOURS = {KINDS[0]: 1, KINDS[1]: 6, KINDS[2]: 24}


class PublicationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tournament_final", "tournament_standings", "tournament_vrs_standings"]
    destination: str
    generation: int = 0
    uid: str
    status: Literal["waiting", "queued", "sent", "disabled", "review"] = "waiting"
    reason: str = "awaiting_final"


class TournamentPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    page: str = Field(min_length=1, max_length=500)
    name: str
    start: date
    end: date
    event_id: str
    participant_count: int | None = None
    editorial_admitted: bool = False
    registered_at: datetime
    checked_at: datetime | None = None
    completed_observed_at: datetime | None = None
    match: MatchNormalized | None = None
    tasks: list[PublicationTask] = Field(default_factory=list)
    source_error: str | None = None


def event_storage_key(page):
    return PREFIX + hashlib.sha256(page.encode()).hexdigest() + ".json"


def final_content_uid(match, destination):
    return f"tournament-final-v1:{destination}:{match.tournament_parent}"


def policy_kind(kind):
    return "results" if kind == "tournament_final" else kind


class Store:
    def __init__(self, client=None, bucket=None):
        self.client = client or storage._client()
        self.bucket = bucket or storage._bucket()

    def read(self, key):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = response["Body"]
            try:
                return json.loads(stream.read()), response.get("ETag")
            finally:
                stream.close()
        except ClientError as exc:
            if storage._is_not_found(exc):
                return None, None
            raise storage.StorageUnavailableError("tournament registry read failed") from exc
        except (BotoCoreError, ValueError, KeyError, TypeError) as exc:
            raise storage.StorageUnavailableError("tournament registry payload unavailable") from exc

    def write(self, key, payload, etag=None):
        try:
            self.client.put_object(Bucket=self.bucket, Key=key,
                Body=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
                ContentType="application/json", **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
        except (ClientError, BotoCoreError) as exc:
            raise storage.StorageUnavailableError("tournament registry write failed; retry required") from exc

    def events(self):
        token, seen, records = None, set(), []
        while True:
            try:
                page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=PREFIX, MaxKeys=1000,
                    **({"ContinuationToken": token} if token else {}))
            except (ClientError, BotoCoreError) as exc:
                raise storage.StorageUnavailableError("tournament registry list failed") from exc
            for item in page.get("Contents", []):
                value, etag = self.read(item["Key"])
                if value is not None:
                    records.append((TournamentPublication.model_validate(value), etag))
            if not page.get("IsTruncated"):
                return records
            token = page.get("NextContinuationToken")
            if not token or token in seen:
                raise storage.StorageUnavailableError("tournament registry pagination incomplete")
            seen.add(token)

    def claim_status(self, uid):
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=storage.claim_key("content_" + storage.safe_storage_part(uid)))
            return head.get("Metadata", {}).get("delivery-state")
        except ClientError as exc:
            if storage._is_not_found(exc):
                return None
            raise storage.StorageUnavailableError("tournament claim read failed") from exc


def destinations(runtime):
    result = {str(c.get("id") or c["name"]): c for c in runtime._iter_channels()}
    if runtime.instagram_publishing_enabled():
        result["instagram"] = {"id": "instagram", "name": "instagram", "platform": "instagram"}
    if runtime.threads_publishing_enabled():
        result["threads"] = {"id": "threads", "name": "threads", "platform": "threads"}
    return result


def publication_policy(runtime):
    if getattr(runtime, "PUBLICATION_CONTROL_ENABLED", False):
        snapshot = asyncio.run(runtime.read_policy())
        if snapshot.revision == 0 and not snapshot.entries:
            raise storage.StorageUnavailableError("owner publication policy not initialized")
        return lambda destination, kind: runtime.policy_status(snapshot, destination, kind)
    return runtime.publication_delivery_allowed


def _new_event(row, runtime, targets, now, policy, editorial_admitted=False):
    page = row["pagename"]
    profile = next((p for p in load_profiles(runtime.TOURNAMENT_PREVIEW_PROFILES_PATH)
                    if p.liquipedia_page == page), None)
    event_id = "event:" + profile.key if profile else page
    record = TournamentPublication(page=page, name=row["name"], start=row["start"], end=row["end"],
        participant_count=row.get("participant_count") or (profile.expected_participant_count if profile else None),
        event_id=event_id, registered_at=now)
    record.editorial_admitted = bool(editorial_admitted or (profile and profile.approved)
                                     or is_tier1_event(row["name"], row.get("prize_pool_usd")))
    if not record.editorial_admitted:
        return record  # Calendar-only event: preserve a before, do not broaden publishing.
    for destination in targets:
        for kind in KINDS:
            allowed, generation = policy(destination, policy_kind(kind))
            uid = (f"tournament-final-v1:{destination}:{page}" if kind == KINDS[0] else
                   f"tournament-standings-v1:{destination}:{page}" if kind == KINDS[1] else
                   f"tournament-vrs-v1:{destination}:{event_id}")
            enabled = allowed and (kind != KINDS[2] or runtime.ENABLE_VRS)
            record.tasks.append(PublicationTask(kind=kind, destination=destination, uid=uid,
                generation=generation, status="waiting" if enabled else "disabled",
                reason="awaiting_final" if enabled else "disabled_by_owner"))
    return record


def register_event(row, runtime, *, now, dry_run=False, store=None, targets=None, policy=None,
                   editorial_admitted=False):
    store = store or Store()
    current, etag = store.read(event_storage_key(row["pagename"]))
    if current is not None:
        return TournamentPublication.model_validate(current)
    record = _new_event(row, runtime, targets if targets is not None else destinations(runtime), now,
                        policy or publication_policy(runtime), editorial_admitted)
    if not dry_run:
        store.write(event_storage_key(record.page), record.model_dump(mode="json"))
    return record


def register_match(match, runtime, *, now=None):
    """Track known primary events and exact LP finals, without enrolling old history."""
    now = now or datetime.now(timezone.utc)
    if not is_valid_match(match)[0] or not is_tier1_lan(match)[0]:
        return False
    profile = event_for_match(match, runtime.TOURNAMENT_PREVIEW_PROFILES_PATH)
    if not profile and not (match.source == "liquipedia" and match.final_identity_confirmed):
        return False
    page = profile.liquipedia_page if profile else match.tournament_parent
    if not page:
        return False
    store = Store()
    existing, etag = store.read(event_storage_key(page))
    if existing is not None:
        record = TournamentPublication.model_validate(existing)
        if not record.editorial_admitted:
            enrolled = _new_event({"pagename": page, "name": record.name, "start": record.start,
                "end": record.end, "participant_count": record.participant_count}, runtime,
                destinations(runtime), now, publication_policy(runtime), editorial_admitted=True)
            record.editorial_admitted = True
            record.tasks = record.tasks or enrolled.tasks
            store.write(event_storage_key(page), record.model_dump(mode="json"), etag)
        return True
    end = max(s.end for s in profile.stages) if profile else date.fromisoformat((match.end_date or match.date or match.start_date)[:10])
    if end < date.fromisoformat(runtime.TOURNAMENT_PUBLICATIONS_START_DATE):
        return False
    start = min(s.start for s in profile.stages) if profile else end
    register_event({"pagename": page, "name": match.tournament_name, "start": start, "end": end},
                   runtime, now=now, store=store, editorial_admitted=True)
    return True


def _before(record, runtime, now):
    snapshot = asyncio.run(runtime.read_vrs_snapshot(record.event_id, "before", "initial"))
    if snapshot is None:
        snapshot = asyncio.run(runtime.read_vrs_snapshot(calendar_baseline_id(record.page), "before", "initial"))
    start = datetime.combine(record.start, time.min, tzinfo=MOSCOW)
    if snapshot is not None:
        validate_before(snapshot, start, now=now)
    return snapshot, start


def _impacts(record, runtime, now, snapshot):
    before, _ = _before(record, runtime, now)
    if before is None:
        raise VRSDataError("baseline_missing")
    # LP dates are start times. Without a provider-confirmed end timestamp, use
    # the end of the last tournament date rather than relabelling a start as end.
    ended = (datetime.fromisoformat(record.match.end_date.replace("Z", "+00:00"))
             if record.match.end_date else datetime.combine(record.end + timedelta(days=1), time.min, tzinfo=MOSCOW))
    effective = datetime.fromisoformat(snapshot.effective_at.replace("Z", "+00:00"))
    if effective.tzinfo is None or effective < ended:
        raise VRSDataError("awaiting_after_snapshot")
    return calculate_impacts(record.match.tournament_placements, before, snapshot)


def _processed(task, runtime, dry_run):
    if asyncio.run(runtime.is_processed("content_" + storage.safe_storage_part(task.uid))):
        return True
    return False if dry_run else asyncio.run(runtime.reconcile_content_delivery(task.uid, task.kind))


def run_publications_job(dry_run, runtime, *, recovery_key=None, now=None, context=None):
    now = now or datetime.now(timezone.utc)
    body = {"job": "tournament_publications", "dry_run": dry_run, "checked_at": now.isoformat(),
            "events": [], "issues": [], "enqueued": 0}
    if not runtime.ENABLE_TOURNAMENT_PUBLICATIONS:
        return {"statusCode": 200, "body": json.dumps({**body, "status": "disabled"})}
    try:
        store, targets, policy = Store(), destinations(runtime), publication_policy(runtime)
        records = {r.page: (r, etag) for r, etag in store.events()}
        profiles = load_profiles(runtime.TOURNAMENT_PREVIEW_PROFILES_PATH)
        today = now.astimezone(MOSCOW).date()
        rows, discovery_save = [], None
        if recovery_key:
            profile = next((p for p in profiles if p.key == recovery_key and p.approved), None)
            if profile is None:
                return runtime._error_response(400, "recovery_profile_unknown")
            rows = [{"pagename": profile.liquipedia_page, "name": profile.key,
                     "start": min(s.start for s in profile.stages), "end": max(s.end for s in profile.stages)}]
        else:
            discovery, discovery_etag = store.read(DISCOVERY_KEY)
            checked = datetime.fromisoformat(discovery["checked_at"]) if discovery else None
            if checked is None or now - checked >= timedelta(hours=1):
                # Profiles supply a second expectation list if the API is down.
                rows = [{"pagename": p.liquipedia_page, "name": p.key,
                         "start": min(s.start for s in p.stages), "end": max(s.end for s in p.stages)}
                        for p in profiles if p.approved and min(s.start for s in p.stages) <= today + timedelta(days=7)
                        and max(s.end for s in p.stages) >= today]
                try:
                    scan = asyncio.run(fetch_preview_calendar(now, require_prize_pool=False, lookback_days=14))
                    rows = list({r["pagename"]: r for r in [*rows, *scan.events]}.values())
                    body["issues"].extend(scan.row_issues)
                    if not scan.row_issues:
                        discovery_save = discovery_etag
                except Exception as exc:
                    body["issues"].append({"stage": "calendar", "error_type": type(exc).__name__})
        for row in rows:
            if (not recovery_key and row["pagename"] not in records
                    and date.fromisoformat(str(row["end"])) < date.fromisoformat(runtime.TOURNAMENT_PUBLICATIONS_START_DATE)):
                continue
            if row["pagename"] not in records:
                record = register_event(row, runtime, now=now, dry_run=dry_run, store=store, targets=targets, policy=policy)
                if dry_run:
                    records[record.page] = (record, None)
                else:
                    saved, etag = store.read(event_storage_key(record.page))
                    records[record.page] = (TournamentPublication.model_validate(saved), etag)
            else:
                record, etag = records[row["pagename"]]
                start, end = date.fromisoformat(str(row["start"])), date.fromisoformat(str(row["end"]))
                count = row.get("participant_count") or record.participant_count
                admission_changed = False
                if not record.editorial_admitted:
                    enrolled = _new_event(row, runtime, targets, now, policy)
                    if enrolled.editorial_admitted:
                        record.editorial_admitted = admission_changed = True
                        record.tasks = record.tasks or enrolled.tasks
                if admission_changed or (record.start, record.end, record.participant_count) != (start, end, count):
                    record.start, record.end, record.participant_count = start, end, count
                    if not dry_run:
                        store.write(event_storage_key(record.page), record.model_dump(mode="json"), etag)
                        _, etag = store.read(event_storage_key(record.page))
                    records[record.page] = record, etag
        if not recovery_key and rows and not dry_run and not body["issues"]:
            store.write(DISCOVERY_KEY, {"checked_at": now.isoformat()}, discovery_save)
        if recovery_key:
            records = {page: value for page, value in records.items() if page == profile.liquipedia_page}
        snapshot, snapshot_error, snapshot_attempted = None, None, False

        def current_snapshot():
            nonlocal snapshot, snapshot_error, snapshot_attempted
            if not snapshot_attempted:
                snapshot_attempted = True
                try:
                    snapshot = asyncio.run(runtime.fetch_vrs_snapshot())
                except Exception as exc:
                    snapshot_error = type(exc).__name__
            if snapshot is None:
                raise VRSDataError("vrs_source_unavailable:" + str(snapshot_error))
            return snapshot

        ordered = sorted(records.values(), key=lambda pair: pair[0].checked_at or pair[0].registered_at)
        source_budget = 4
        for record_index, (record, etag) in enumerate(ordered):
            # A complete record stays inspectable but causes no source requests.
            pending = [t for t in record.tasks if t.status not in {"sent", "disabled"}]
            if context is not None and hasattr(context, "get_remaining_time_in_millis") and context.get_remaining_time_in_millis() < 35000:
                body["issues"].append({"stage": "budget", "page": record.page})
                body["events"].extend(_report(r, now) for r, _ in ordered[record_index:])
                break
            if runtime.ENABLE_VRS and today < record.start:
                try:
                    before, start = _before(record, runtime, now)
                    if before is None:
                        before = current_snapshot()
                        validate_before(before, start, now=now)
                        if not dry_run:
                            asyncio.run(runtime.write_vrs_snapshot(calendar_baseline_id(record.page), "before", before, initial=True))
                except Exception as exc:
                    record.source_error = "before:" + type(exc).__name__
                    body["issues"].append({"stage": "before", "page": record.page, "error_type": type(exc).__name__})
            if not pending:
                body["events"].append(_report(record, now))
                continue
            if today >= record.start and source_budget > 0 and any(t.status != "review" for t in pending):
                source_budget -= 1
                try:
                    matches = asyncio.run(fetch_finished_matches(100, parent=record.page))
                    finals, rejected = confirm_liquipedia_final_candidates(matches)
                    if len(finals) == 1:
                        record.match = finals[0]
                        record.name = record.match.tournament_name
                        record.completed_observed_at = record.completed_observed_at or now
                        record.source_error = None
                    elif len(finals) > 1:
                        record.source_error = "ambiguous_final"
                    else:
                        reasons = sorted(set(rejected.values()))[:3]
                        record.source_error = "awaiting_confirmed_final" + (":" + ",".join(reasons) if reasons else "")
                except Exception as exc:
                    record.source_error = "final_source:" + type(exc).__name__
            impacts = None
            for task in pending:
                if _processed(task, runtime, dry_run):
                    task.status, task.reason = "sent", "confirmed_delivery"
                    continue
                if task.status == "review":
                    continue
                claim_status = store.claim_status(task.uid)
                if claim_status in {"attempting", "uncertain", "blocked"}:
                    task.status, task.reason = "review", "delivery_outcome_unknown"
                    continue
                allowed, generation = policy(task.destination, policy_kind(task.kind))
                if task.destination not in targets or not allowed or generation > task.generation:
                    task.status, task.reason = "disabled", "disabled_by_owner"
                    continue
                if record.match is None:
                    task.reason = record.source_error or "awaiting_final"
                    continue
                match = record.match
                valid, reason = is_valid_match(match)
                admitted, admission_reason = is_tier1_lan(match)
                if not valid or (not admitted and not (record.editorial_admitted
                        and admission_reason in {"not_tier1", "lan_unconfirmed"})):
                    task.status, task.reason = "disabled", "outside_editorial_scope:" + str(reason or admission_reason)
                    continue
                if task.kind == KINDS[0]:
                    ready = runtime.can_render_final_card(match)
                    task.reason = "awaiting_played_maps" if not ready else "ready"
                elif task.kind == KINDS[1]:
                    ready = runtime._can_publish_tournament_standings(match)
                    if record.participant_count and len(match.tournament_placements) != record.participant_count:
                        ready = False
                    task.reason = "awaiting_complete_payouts" if not ready else "ready"
                else:
                    try:
                        if record.participant_count and len(match.tournament_placements) != record.participant_count:
                            raise VRSDataError("awaiting_complete_participants")
                        if impacts is None:
                            impacts = _impacts(record, runtime, now, current_snapshot())
                        ready = runtime.can_render_tournament_vrs(impacts)
                        task.reason = "ready" if ready else "vrs_incomplete"
                    except Exception as exc:
                        ready = False
                        task.reason = str(exc) if isinstance(exc, VRSDataError) else "vrs:" + type(exc).__name__
                if not ready:
                    continue
                if not dry_run:
                    if task.kind == KINDS[2]:
                        asyncio.run(runtime.write_vrs_snapshot(record.event_id, "after", current_snapshot()))
                    queued = asyncio.run(runtime.enqueue_result_delivery(match, task.destination,
                        targets[task.destination].get("name", task.destination), content_type=task.kind,
                        vrs_impacts=tuple(impacts or ()) if task.kind == KINDS[2] else (),
                        generation=task.generation, content_uid=task.uid))
                    body["enqueued"] += int(queued)
                task.status, task.reason = "queued", "ready_for_delivery" if not dry_run else "would_enqueue"
                if not dry_run:
                    queued_payload, _ = store.read(storage.result_outbox_key(match, task.destination, task.kind))
                    if queued_payload and queued_payload.get("attempt_count", 0):
                        task.reason = "delivery_retry:" + str(queued_payload["attempt_count"])
            record.checked_at = now
            if not dry_run:
                store.write(event_storage_key(record.page), record.model_dump(mode="json"), etag)
            body["events"].append(_report(record, now))
        body["status"] = "gaps" if body["issues"] or any(row["overdue"] for row in body["events"]) else "ok"
        if not dry_run:
            _, report_etag = store.read(REPORT_KEY)
            store.write(REPORT_KEY, body, report_etag)
            overdue = [r for r in body["events"] if r["overdue"]]
            if overdue or body["issues"]:
                reasons = "; ".join(f"{r['name']}: " + ", ".join(r["overdue"]) for r in overdue[:5])
                runtime._notify_admin("tournament_publications_incomplete",
                    "Не завершены публикации турниров. " + (reasons or "Ошибка календаря/сохранения снимков") +
                    ". Подробности: diagnostics/tournament-publications/latest.json")
        return {"statusCode": 200 if not body["issues"] else 503, "body": json.dumps(body, ensure_ascii=False)}
    except Exception as exc:
        if not dry_run:
            runtime._notify_admin("tournament_publications_unavailable", "Не завершена проверка обязательных публикаций; реестр сохранён для повтора.")
        return {"statusCode": 503, "body": json.dumps({**body, "status": "unavailable", "error_type": type(exc).__name__})}


def _report(record, now):
    calendar_end = datetime.combine(record.end + timedelta(days=1), time.min, tzinfo=MOSCOW)
    due = min(record.completed_observed_at or calendar_end, calendar_end)
    overdue = [f"{t.destination}/{t.kind}: {t.reason}" for t in record.tasks
               if t.status not in {"sent", "disabled"} and now >= due + timedelta(hours=ALERT_HOURS[t.kind])]
    return {"page": record.page, "name": record.name, "start": str(record.start), "end": str(record.end),
            "source_error": record.source_error, "overdue": overdue,
            "tasks": [t.model_dump() for t in record.tasks]}
