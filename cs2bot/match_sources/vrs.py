"""Official VRS snapshot contract and before/after calculations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    TournamentPlacement,
    TournamentVRSImpact,
    VRSRankingSnapshot,
    VRSTeamSnapshot,
)


class VRSDataError(ValueError):
    """Raised when an official VRS response cannot be trusted."""


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _timestamp(value: str, field: str) -> str:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise VRSDataError(f"VRS {field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise VRSDataError(f"VRS {field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _items(payload: Any) -> tuple[Any, ...]:
    if isinstance(payload, list):
        return tuple(payload)
    if not isinstance(payload, dict):
        return ()
    for key in ("teams", "rankings", "standings", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(value)
    return ()


def normalize_snapshot(payload: Any, *, source: str, fetched_at: str | None = None) -> VRSRankingSnapshot:
    """Normalize a provider response; version metadata is deliberately mandatory."""
    if not isinstance(payload, dict):
        raise VRSDataError("VRS response must be an object")
    version = _text(payload.get("version") or payload.get("period") or payload.get("ranking_period"))
    effective_raw = _text(payload.get("effective_at") or payload.get("effectiveAt") or payload.get("published_at"))
    if not version or not effective_raw:
        raise VRSDataError("VRS snapshot lacks version or effective_at")
    effective_at = _timestamp(effective_raw, "effective_at")
    fetched = fetched_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    teams: list[VRSTeamSnapshot] = []
    seen: set[str] = set()
    for item in _items(payload):
        if not isinstance(item, dict):
            raise VRSDataError("VRS team entry is not an object")
        nested_team = item.get("team") if isinstance(item.get("team"), dict) else {}
        team_id = _text(item.get("team_id") or item.get("id") or nested_team.get("id"))
        team = item.get("team") if isinstance(item.get("team"), dict) else item
        name = _text(team.get("name") or team.get("full_name")) if isinstance(team, dict) else None
        points = item.get("points")
        rank = item.get("rank") or item.get("position")
        if not team_id or not name or isinstance(points, bool) or isinstance(rank, bool):
            raise VRSDataError("VRS team entry is incomplete")
        try:
            points_int, rank_int = int(points), int(rank)
        except (TypeError, ValueError) as exc:
            raise VRSDataError("VRS points/rank are invalid") from exc
        if points_int < 0 or rank_int < 1 or team_id in seen:
            raise VRSDataError("VRS team entry has invalid or duplicate identity")
        seen.add(team_id)
        teams.append(VRSTeamSnapshot(team_id=team_id, team_name=name, points=points_int, rank=rank_int))
    if not teams:
        raise VRSDataError("VRS snapshot contains no teams")
    return VRSRankingSnapshot(source=source, version=version, effective_at=effective_at, fetched_at=fetched, teams=teams)


def calculate_impacts(
    placements: Iterable[TournamentPlacement],
    before: VRSRankingSnapshot,
    after: VRSRankingSnapshot,
) -> list[TournamentVRSImpact]:
    """Join placements to two snapshots from the same source without guessing."""
    if before.source != after.source:
        raise VRSDataError("VRS snapshots come from different sources")
    if before.version == after.version or _timestamp(before.effective_at, "effective_at") >= _timestamp(after.effective_at, "effective_at"):
        raise VRSDataError("after VRS snapshot is not newer than baseline")
    before_by_name = {item.team_name.casefold(): item for item in before.teams}
    after_by_name = {item.team_name.casefold(): item for item in after.teams}
    impacts: list[TournamentVRSImpact] = []
    for placement in placements:
        key = placement.team_name.casefold()
        old, new = before_by_name.get(key), after_by_name.get(key)
        if old is None or new is None or old.team_id != new.team_id:
            raise VRSDataError(f"VRS data is incomplete for {placement.team_name}")
        impacts.append(TournamentVRSImpact(
            placement=placement.placement,
            team_name=placement.team_name,
            team_id=new.team_id,
            before_points=old.points,
            after_points=new.points,
            before_rank=old.rank,
            after_rank=new.rank,
            points_delta=new.points - old.points,
            rank_delta=old.rank - new.rank,
            source=after.source,
            before_version=before.version,
            after_version=after.version,
        ))
    if not impacts:
        raise VRSDataError("VRS impact has no tournament teams")
    return impacts
