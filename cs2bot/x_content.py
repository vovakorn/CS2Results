"""Pure, length-bounded text builders for X publication drafts.

These helpers accept already-prepared match data and image collections. They do
not render media, publish posts, or call external services.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


X_TEXT_LIMIT = 240
X_IMAGE_LIMIT = 4


class XContentError(ValueError):
    """The prepared data cannot produce a complete X post."""


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _required_text(value: Any, name: str) -> str:
    if value is None:
        raise XContentError(f"{name} is required")
    text = str(value).strip()
    if not text:
        raise XContentError(f"{name} is required")
    return text


def _image_count(images: Sequence[object]) -> int:
    try:
        count = len(images)
    except TypeError as exc:
        raise XContentError("images must be a sized collection") from exc
    if count == 0:
        raise XContentError("at least one image is required")
    if count > X_IMAGE_LIMIT:
        raise XContentError(f"at most {X_IMAGE_LIMIT} images are supported")
    return count


def _date_label(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _required_text(value, "date")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise XContentError("date must be an ISO date or datetime") from exc
    return parsed.date().isoformat()


def _complete_text(core: str, optional_lines: Sequence[str] = ()) -> str:
    """Add secondary explanations only when the mandatory text already fits."""
    if len(core) > X_TEXT_LIMIT:
        raise XContentError(
            f"required X text is {len(core)} characters; limit is {X_TEXT_LIMIT}"
        )
    text = core
    for line in optional_lines:
        clean = str(line).strip()
        if not clean:
            continue
        candidate = f"{text}\n{clean}"
        if len(candidate) <= X_TEXT_LIMIT:
            text = candidate
    return text


def _team_names(match: Any) -> tuple[str, str]:
    return (
        _required_text(_field(match, "team1_name"), "team1_name"),
        _required_text(_field(match, "team2_name"), "team2_name"),
    )


def _scheduled_time(match: Any, timezone_name: str) -> str:
    raw = _required_text(_field(match, "scheduled_at"), "scheduled_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise XContentError("scheduled_at must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        local = parsed.astimezone(ZoneInfo(timezone_name))
    except (KeyError, TypeError, ValueError) as exc:
        raise XContentError("timezone_name is invalid") from exc
    return local.strftime("%H:%M")


def _result_date(match: Any) -> str:
    value = _field(match, "end_date") or _field(match, "date")
    if value is None:
        raise XContentError("result date is required")
    return _date_label(value)


def _score(match: Any) -> str:
    score1 = _field(match, "score1")
    score2 = _field(match, "score2")
    if (
        score1 is None
        or score2 is None
        or str(score1).strip() == ""
        or str(score2).strip() == ""
    ):
        raise XContentError("both scores are required")
    return f"{score1}:{score2}"


def _tournament_line(match: Any) -> str | None:
    value = _field(match, "tournament_name")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_x_schedule_text(
    matches: Sequence[Any],
    schedule_date: date | datetime | str,
    images: Sequence[object],
    timezone_name: str = "Europe/Moscow",
) -> str:
    """Build a schedule post while preserving every team name and match time."""
    _image_count(images)
    if not matches:
        raise XContentError("at least one schedule match is required")

    date_text = _date_label(schedule_date)
    match_lines: list[str] = []
    optional_lines: list[str] = []
    for match in matches:
        team1, team2 = _team_names(match)
        match_lines.append(f"{_scheduled_time(match, timezone_name)} {team1} — {team2}")
        tournament = _tournament_line(match)
        if tournament:
            optional_lines.append(tournament)
    core = f"Матчи CS2 · {date_text}\n" + "\n".join(match_lines)
    return _complete_text(core, optional_lines)


def build_x_result_text(match: Any, images: Sequence[object]) -> str:
    """Build one result post with complete teams, score, and match date."""
    _image_count(images)
    team1, team2 = _team_names(match)
    core = (
        f"CS2 · {_result_date(match)}\n"
        f"{team1} {_score(match)} {team2}"
    )
    tournament = _tournament_line(match)
    return _complete_text(core, [tournament] if tournament else ())


def build_x_digest_text(
    matches: Sequence[Any],
    digest_date: date | datetime | str,
    images: Sequence[object],
) -> str:
    """Build an evening recap without dropping any match from the prepared set."""
    _image_count(images)
    if not matches:
        raise XContentError("at least one digest match is required")

    lines = [f"Итоги CS2 · {_date_label(digest_date)}"]
    optional_lines: list[str] = []
    for match in matches:
        team1, team2 = _team_names(match)
        lines.append(f"{_result_date(match)} {team1} {_score(match)} {team2}")
        tournament = _tournament_line(match)
        if tournament:
            optional_lines.append(tournament)
    return _complete_text("\n".join(lines), optional_lines)
