from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal

from . import config as source_config
from .filters import is_tier1_lan, is_valid_match
from .models import MatchNormalized, SourceUnavailableError
from .storage import is_channel_processed, is_match_processed, mark_channel_processed

ConcreteSourceName = Literal["pandascore", "liquipedia"]
SourceName = Literal["auto", "pandascore", "liquipedia"]

logger = logging.getLogger(__name__)


async def _fetch_from_source(source: ConcreteSourceName, limit: int) -> list[MatchNormalized]:
    if source == "pandascore":
        from .sources import pandascore_source

        return await pandascore_source.fetch_finished_matches(limit=limit)
    from .sources import liquipedia_source

    return await liquipedia_source.fetch_finished_matches(limit=limit)


def _source_is_usable(
    source: str,
    matches: list[MatchNormalized],
    require_fresh: bool,
) -> tuple[bool, str]:
    if not matches:
        logger.warning("source=%s status=empty", source)
        return False, "empty"
    valid_matches = [match for match in matches if is_valid_match(match)[0]]
    if not valid_matches:
        logger.warning("source=%s status=invalid reason=no_valid_matches", source)
        return False, "invalid"

    fresh, _ = log_source_freshness(source, valid_matches)
    if require_fresh and not fresh:
        return False, "stale_or_undated"
    return True, "usable"


async def _choose_source(
    source: SourceName,
    limit: int,
    require_fresh: bool = True,
) -> tuple[str, list[MatchNormalized]]:
    if source in {"pandascore", "liquipedia"}:
        matches = await _fetch_from_source(source, limit)
        logger.info("source=%s fetched=%s", source, len(matches))
        usable, reason = _source_is_usable(source, matches, require_fresh=require_fresh)
        if not usable and (require_fresh or reason != "empty"):
            raise SourceUnavailableError(f"{source} is not usable: {reason}")
        return source, matches

    primary_error: str | None = None
    try:
        matches = await _fetch_from_source("pandascore", limit)
        logger.info("source=pandascore fetched=%s", len(matches))
        usable, reason = _source_is_usable("pandascore", matches, require_fresh=require_fresh)
        if usable:
            return "pandascore", matches
        primary_error = reason
        if not source_config.ENABLE_LIQUIPEDIA_FALLBACK:
            logger.warning("source=pandascore status=%s fallback=disabled", reason)
            if require_fresh:
                raise SourceUnavailableError(f"pandascore is not usable: {reason}")
            return "pandascore", matches
        logger.warning("source=pandascore status=%s fallback=liquipedia", reason)
    except SourceUnavailableError as exc:
        primary_error = str(exc)
        if not source_config.ENABLE_LIQUIPEDIA_FALLBACK:
            logger.warning("source=pandascore status=unavailable fallback=disabled error=%s", exc)
            raise
        logger.warning("source=pandascore status=unavailable fallback=liquipedia error=%s", exc)

    try:
        matches = await _fetch_from_source("liquipedia", limit)
        logger.info("source=liquipedia fetched=%s", len(matches))
        usable, reason = _source_is_usable("liquipedia", matches, require_fresh=require_fresh)
        if not usable and (require_fresh or reason != "empty"):
            raise SourceUnavailableError(f"liquipedia is not usable: {reason}")
        return "liquipedia", matches
    except SourceUnavailableError as exc:
        raise SourceUnavailableError(
            f"no usable match source; pandascore={primary_error or 'unavailable'}; liquipedia={exc}"
        ) from exc


def _parse_match_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_match_datetime(matches: list[MatchNormalized]) -> datetime | None:
    parsed_dates = [
        parsed
        for match in matches
        if (parsed := _parse_match_datetime(match.end_date or match.date or match.start_date)) is not None
    ]
    return max(parsed_dates) if parsed_dates else None


def is_match_fresh(match: MatchNormalized, now: datetime | None = None) -> bool:
    parsed = _parse_match_datetime(match.end_date or match.date or match.start_date)
    if parsed is None:
        return False
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_hours = (reference.astimezone(timezone.utc) - parsed).total_seconds() / 3600
    return (
        -source_config.MAX_SOURCE_FUTURE_SKEW_HOURS
        <= age_hours
        <= source_config.MAX_SOURCE_STALENESS_HOURS
    )


def log_source_freshness(
    source: str,
    matches: list[MatchNormalized],
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    latest = latest_match_datetime(matches)
    if latest is None:
        logger.warning("event=source_freshness_unknown source=%s reason=missing_match_dates", source)
        return False, None

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_hours = (reference.astimezone(timezone.utc) - latest).total_seconds() / 3600
    if age_hours < -source_config.MAX_SOURCE_FUTURE_SKEW_HOURS:
        logger.warning(
            "event=source_future_timestamp source=%s latest_match_at=%s skew_hours=%.1f threshold_hours=%s",
            source,
            latest.isoformat().replace("+00:00", "Z"),
            abs(age_hours),
            source_config.MAX_SOURCE_FUTURE_SKEW_HOURS,
        )
        return False, age_hours

    is_fresh = age_hours <= source_config.MAX_SOURCE_STALENESS_HOURS
    log_method = logger.info if is_fresh else logger.warning
    log_method(
        "event=%s source=%s latest_match_at=%s age_hours=%.1f threshold_hours=%s",
        "source_fresh" if is_fresh else "source_stale",
        source,
        latest.isoformat().replace("+00:00", "Z"),
        age_hours,
        source_config.MAX_SOURCE_STALENESS_HOURS,
    )
    return is_fresh, age_hours


def apply_quality_filters(
    matches: list[MatchNormalized],
    include_filtered: bool = False,
) -> tuple[list[MatchNormalized], list[MatchNormalized], int]:
    valid_matches: list[MatchNormalized] = []
    output: list[MatchNormalized] = []

    for match in matches:
        valid, reason = is_valid_match(match)
        if not valid:
            match.filter_reason = reason
            if include_filtered:
                output.append(match)
            continue

        valid_matches.append(match)
        tier1_lan, filter_reason = is_tier1_lan(match)
        match.is_tier1_lan = tier1_lan
        match.filter_reason = filter_reason
        if tier1_lan or include_filtered:
            output.append(match)

    return output, valid_matches, sum(1 for match in output if match.is_tier1_lan)


async def get_new_finished_matches(
    limit: int = 30,
    source: SourceName | None = None,
    dry_run: bool = False,
    include_filtered: bool = False,
    check_processed: bool = True,
    rejected_matches: list[MatchNormalized] | None = None,
) -> list[MatchNormalized]:
    selected_source = source or source_config.MATCH_SOURCE
    if selected_source not in {"auto", "pandascore", "liquipedia"}:
        raise ValueError("--source must be auto, pandascore, or liquipedia")

    logger.info("match_fetcher start source=%s limit=%s dry_run=%s", selected_source, limit, dry_run)
    fetch_limit = min(max(limit * 3, limit), 100)
    require_fresh = not (dry_run and source_config.ALLOW_STALE_IN_DRY_RUN)
    used_source, fetched = await _choose_source(
        selected_source,
        fetch_limit,
        require_fresh=require_fresh,
    )
    if require_fresh:
        fresh_matches = [match for match in fetched if is_match_fresh(match)]
        dropped = len(fetched) - len(fresh_matches)
        if dropped:
            logger.warning(
                "event=stale_matches_dropped source=%s dropped=%s",
                used_source,
                dropped,
            )
        fetched = fresh_matches
    filtered, valid_matches, tier1_lan_count = apply_quality_filters(fetched, include_filtered=include_filtered)
    if rejected_matches is not None:
        rejected_matches.extend(match for match in valid_matches if not match.is_tier1_lan)

    new_matches: list[MatchNormalized] = []
    for match in filtered[:limit]:
        if not match.is_tier1_lan and not include_filtered:
            continue
        if dry_run or not check_processed:
            new_matches.append(match)
            continue
        if await is_match_processed(match):
            continue
        new_matches.append(match)

    logger.info(
        "source=%s valid=%s tier1_lan=%s new=%s",
        used_source,
        len(valid_matches),
        tier1_lan_count,
        len(new_matches),
    )
    return new_matches


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


async def _filter_unprocessed_for_channels(
    matches: list[MatchNormalized],
    channel_names: list[str],
) -> list[MatchNormalized]:
    new_matches: list[MatchNormalized] = []
    for match in matches:
        for channel_name in channel_names:
            if not await is_channel_processed(match, channel_name):
                new_matches.append(match)
                break
    return new_matches


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        matches = await get_new_finished_matches(
            limit=args.limit,
            source=args.source,
            dry_run=args.dry_run,
            include_filtered=args.include_filtered,
            check_processed=not args.channels,
        )
    except SourceUnavailableError as exc:
        logger.error("source=%s status=unavailable error=%s", args.source, exc)
        matches = []

    if args.channels and not args.dry_run:
        matches = await _filter_unprocessed_for_channels(matches, args.channels)

    print(json.dumps(matches, default=_json_default, ensure_ascii=False, indent=2))

    if args.mark_processed:
        if args.dry_run:
            logger.info("dry_run=true mark_processed=skipped")
        elif not args.channels:
            logger.error("--mark-processed requires at least one --channel")
            return 2
        else:
            marked = 0
            for match in matches:
                if match.is_tier1_lan:
                    for channel_name in args.channels:
                        await mark_channel_processed(match, channel_name)
                        marked += 1
            logger.info("marked_processed=%s channels=%s", marked, ",".join(args.channels))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch recently finished CS2 matches.")
    parser.add_argument(
        "--source",
        choices=["auto", "pandascore", "liquipedia"],
        default=os.getenv("MATCH_SOURCE", "auto"),
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-filtered", action="store_true")
    parser.add_argument("--mark-processed", action="store_true")
    parser.add_argument(
        "--channel",
        action="append",
        dest="channels",
        help="Channel name for per-channel Object Storage deduplication. Can be repeated.",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
