"""Adapter for Valve's official versioned VRS snapshots on GitHub."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import requests

from .. import config
from ..models import VRSRankingSnapshot, VRSTeamSnapshot
from ..vrs import VRSDataError


class VRSUnavailableError(RuntimeError):
    pass


_SNAPSHOT_RE = re.compile(r"^standings_(?P<region>[a-z]+)_(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})\.md$")
_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*([^|]+?)\s*\|.*$")
_DETAIL_RE = re.compile(r"details/\d{4}_\d{2}_\d{2}/\d+--([^-/]+)--")


def _github_url(path: str) -> str:
    return f"https://api.github.com/repos/{config.VRS_GITHUB_REPO}/contents/{path}?ref={config.VRS_GITHUB_BRANCH}"


def _team_id(name: str, row: str) -> str:
    detail = _DETAIL_RE.search(row)
    if detail:
        return detail.group(1).casefold()
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value or name.casefold()


def _request_json(url: str) -> Any:
    response = requests.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _request_text(url: str) -> str:
    response = requests.get(url, headers={"Accept": "text/plain"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def _latest_snapshot_file() -> tuple[str, str, str]:
    if not config.ENABLE_VRS:
        raise VRSUnavailableError("VRS source is disabled")
    path = f"{config.VRS_VIEWS_PATH}/{datetime.now(timezone.utc).year}"
    entries = _request_json(_github_url(path))
    if not isinstance(entries, list):
        raise VRSDataError("Valve VRS GitHub directory response is invalid")
    candidates: list[tuple[datetime, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, sha, download_url = entry.get("name"), entry.get("sha"), entry.get("download_url")
        if not isinstance(name, str) or not isinstance(sha, str) or not isinstance(download_url, str):
            continue
        match = _SNAPSHOT_RE.fullmatch(name)
        if not match or match.group("region") != config.VRS_REGION:
            continue
        effective = datetime(int(match.group("year")), int(match.group("month")), int(match.group("day")), tzinfo=timezone.utc)
        candidates.append((effective, f"{name}:{sha}", download_url))
    if not candidates:
        raise VRSUnavailableError("no Valve VRS snapshots found")
    effective, version, download_url = max(candidates, key=lambda item: item[0])
    return effective.isoformat().replace("+00:00", "Z"), version, download_url


def _parse_snapshot(markdown: str, *, effective_at: str, version: str) -> VRSRankingSnapshot:
    teams: list[VRSTeamSnapshot] = []
    seen: set[str] = set()
    for raw_line in markdown.splitlines():
        match = _ROW_RE.match(raw_line.strip())
        if not match:
            continue
        rank, points, name = int(match.group(1)), int(float(match.group(2))), match.group(3).strip()
        if not name:
            continue
        team_id = _team_id(name, raw_line)
        if team_id in seen:
            raise VRSDataError(f"duplicate Valve VRS team {name}")
        seen.add(team_id)
        teams.append(VRSTeamSnapshot(team_id=team_id, team_name=name, points=points, rank=rank))
    if not teams:
        raise VRSDataError("Valve VRS snapshot contains no teams")
    return VRSRankingSnapshot(
        source=config.VRS_SOURCE_NAME,
        version=version,
        effective_at=effective_at,
        fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        teams=teams,
    )


def _request() -> VRSRankingSnapshot:
    effective_at, version, download_url = _latest_snapshot_file()
    return _parse_snapshot(_request_text(download_url), effective_at=effective_at, version=version)


async def fetch_snapshot() -> VRSRankingSnapshot:
    return await asyncio.to_thread(_request)
