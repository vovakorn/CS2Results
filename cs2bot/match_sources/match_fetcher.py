from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from . import config as source_config
from .filters import is_tier1_lan, is_valid_match
from .models import MatchNormalized, SourceUnavailableError
from .storage import is_channel_processed, is_match_processed, mark_channel_processed

ConcreteSourceName = Literal["pandascore", "liquipedia"]
SourceName = Literal["auto", "pandascore", "liquipedia"]

logger = logging.getLogger(__name__)


SHADOW_MATCH_TIME_WINDOW = timedelta(hours=12)


def _shadow_match_teams(match: MatchNormalized) -> tuple[str, str]:
    return tuple(
        sorted(
            (
                match._identity_part(match.team1_name),
                match._identity_part(match.team2_name),
            )
        )
    )


def _shadow_match_datetime(match: MatchNormalized) -> datetime | None:
    """Use scheduled/start time so matches crossing midnight stay comparable."""
    return _parse_match_datetime(match.start_date or match.date or match.end_date)


def _score_by_team(match: MatchNormalized) -> dict[str, int | None]:
    return {
        match._identity_part(match.team1_name): match.score1,
        match._identity_part(match.team2_name): match.score2,
    }


def _is_eligible_liquipedia_final(match: MatchNormalized) -> bool:
    """Accept a final only when its complete card data came from Liquipedia."""
    return (
        match.source == "liquipedia"
        and match.is_final
        and match.winner_prize_usd is not None
        and 3 <= len(match.maps) <= 5
        and all(item.score1 is not None and item.score2 is not None for item in match.maps)
    )


def _same_match(left: MatchNormalized, right: MatchNormalized) -> bool:
    left_time = _shadow_match_datetime(left)
    right_time = _shadow_match_datetime(right)
    return (
        left_time is not None
        and right_time is not None
        and _shadow_match_teams(left) == _shadow_match_teams(right)
        and abs(left_time - right_time) <= SHADOW_MATCH_TIME_WINDOW
    )


def _replace_with_liquipedia_finals(
    primary_matches: list[MatchNormalized],
    liquipedia_matches: list[MatchNormalized],
) -> list[MatchNormalized]:
    """Use Liquipedia as the complete source for eligible final cards only."""
    merged = list(primary_matches)
    replacements = additions = 0
    for final in liquipedia_matches:
        if not _is_eligible_liquipedia_final(final):
            continue
        index = next((i for i, match in enumerate(merged) if _same_match(match, final)), None)
        if index is None:
            merged.append(final)
            additions += 1
        else:
            merged[index] = final
            replacements += 1
    logger.info(
        "event=liquipedia_final_card_selection replacements=%s additions=%s",
        replacements,
        additions,
    )
    return merged


async def _apply_liquipedia_final_card_selection(
    primary_matches: list[MatchNormalized],
    limit: int,
    require_fresh: bool,
) -> list[MatchNormalized]:
    if not source_config.ENABLE_LIQUIPEDIA_FINAL_CARDS:
        return primary_matches
    if not source_config.LIQUIPEDIA_API_KEY:
        logger.warning("event=liquipedia_final_card_skipped reason=missing_credentials")
        return primary_matches
    try:
        matches = await _fetch_from_source("liquipedia", limit)
        if require_fresh:
            matches = [match for match in matches if is_match_fresh(match)]
        return _replace_with_liquipedia_finals(primary_matches, matches)
    except Exception as exc:
        logger.warning("event=liquipedia_final_card_skipped error_type=%s", type(exc).__name__)
        return primary_matches


def compare_source_matches(
    primary_matches: list[MatchNormalized],
    liquipedia_matches: list[MatchNormalized],
) -> dict[str, int]:
    """Compare providers without affecting source selection or publication."""
    shadow_by_teams: dict[tuple[str, str], list[MatchNormalized]] = {}
    for match in liquipedia_matches:
        shadow_by_teams.setdefault(_shadow_match_teams(match), []).append(match)

    matched = 0
    score_mismatches = 0
    best_of_mismatches = 0
    tier_mismatches = 0
    liquipedia_map_coverage = 0
    liquipedia_technical_results = 0

    for primary in primary_matches:
        primary_datetime = _shadow_match_datetime(primary)
        if primary_datetime is None:
            continue
        candidates = shadow_by_teams.get(_shadow_match_teams(primary), [])
        timed_candidates = [
            candidate
            for candidate in candidates
            if (candidate_datetime := _shadow_match_datetime(candidate)) is not None
            and abs(candidate_datetime - primary_datetime) <= SHADOW_MATCH_TIME_WINDOW
        ]
        candidates = timed_candidates
        if not candidates:
            continue
        shadow = min(
            candidates,
            key=lambda candidate: abs(_shadow_match_datetime(candidate) - primary_datetime),
        )
        shadow_by_teams[_shadow_match_teams(primary)].remove(shadow)
        matched += 1
        if _score_by_team(primary) != _score_by_team(shadow):
            score_mismatches += 1
        if (
            primary.best_of is not None
            and shadow.best_of is not None
            and primary.best_of != shadow.best_of
        ):
            best_of_mismatches += 1
        if (
            primary.tournament_tier is not None
            and shadow.tournament_tier is not None
            and primary.tournament_tier != shadow.tournament_tier
        ):
            tier_mismatches += 1
        if shadow.maps:
            liquipedia_map_coverage += 1
        if shadow.forfeit:
            liquipedia_technical_results += 1

    return {
        "primary_count": len(primary_matches),
        "liquipedia_count": len(liquipedia_matches),
        "matched": matched,
        "primary_only": len(primary_matches) - matched,
        "liquipedia_only": len(liquipedia_matches) - matched,
        "score_mismatches": score_mismatches,
        "best_of_mismatches": best_of_mismatches,
        "tier_mismatches": tier_mismatches,
        "liquipedia_map_coverage": liquipedia_map_coverage,
        "liquipedia_technical_results": liquipedia_technical_results,
    }


async def _run_liquipedia_shadow(
    primary_matches: list[MatchNormalized],
    limit: int,
    require_fresh: bool,
) -> dict[str, int] | None:
    if not source_config.ENABLE_LIQUIPEDIA_SHADOW:
        return None
    if not source_config.LIQUIPEDIA_API_KEY:
        logger.warning("event=liquipedia_shadow_skipped reason=missing_credentials")
        return None

    try:
        shadow_matches = await _fetch_from_source("liquipedia", limit)
        usable, reason = _source_is_usable(
            "liquipedia_shadow",
            shadow_matches,
            require_fresh=require_fresh,
        )
        if not usable:
            logger.warning("event=liquipedia_shadow_skipped reason=%s", reason)
            return None
        valid_primary = [match for match in primary_matches if is_valid_match(match)[0]]
        comparison = compare_source_matches(valid_primary, shadow_matches)
        logger.info(
            (
                "event=liquipedia_shadow_comparison primary_count=%s liquipedia_count=%s "
                "matched=%s primary_only=%s liquipedia_only=%s score_mismatches=%s "
                "best_of_mismatches=%s tier_mismatches=%s liquipedia_map_coverage=%s "
                "liquipedia_technical_results=%s"
            ),
            comparison["primary_count"],
            comparison["liquipedia_count"],
            comparison["matched"],
            comparison["primary_only"],
            comparison["liquipedia_only"],
            comparison["score_mismatches"],
            comparison["best_of_mismatches"],
            comparison["tier_mismatches"],
            comparison["liquipedia_map_coverage"],
            comparison["liquipedia_technical_results"],
        )
        return comparison
    except Exception as exc:
        logger.warning(
            "event=liquipedia_shadow_failed error_type=%s",
            type(exc).__name__,
        )
        return None


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
        for value in (match.end_date, match.date, match.start_date)
        if (parsed := _parse_match_datetime(value)) is not None
    ]
    return max(parsed_dates) if parsed_dates else None


def is_match_fresh(match: MatchNormalized, now: datetime | None = None) -> bool:
    timestamps = [
        parsed
        for value in (match.end_date, match.date, match.start_date)
        if (parsed := _parse_match_datetime(value)) is not None
    ]
    if not timestamps:
        return False
    parsed = max(timestamps)
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

    tier_counts = {
        (tier, selected): sum(
            1
            for match in valid_matches
            if match.source == "pandascore"
            and (match.tournament_tier or "unknown") == tier
            and match.is_tier1_lan is selected
        )
        for tier in ("s", "a", "b", "c", "d", "unknown")
        for selected in (True, False)
    }
    if valid_matches and any(match.source == "pandascore" for match in valid_matches):
        logger.info(
            (
                "event=pandascore_tier_diagnostics "
                "s_selected=%s s_rejected=%s a_selected=%s a_rejected=%s "
                "b_selected=%s b_rejected=%s c_selected=%s c_rejected=%s "
                "d_selected=%s d_rejected=%s unknown_selected=%s unknown_rejected=%s"
            ),
            tier_counts[("s", True)],
            tier_counts[("s", False)],
            tier_counts[("a", True)],
            tier_counts[("a", False)],
            tier_counts[("b", True)],
            tier_counts[("b", False)],
            tier_counts[("c", True)],
            tier_counts[("c", False)],
            tier_counts[("d", True)],
            tier_counts[("d", False)],
            tier_counts[("unknown", True)],
            tier_counts[("unknown", False)],
        )

    return output, valid_matches, sum(1 for match in output if match.is_tier1_lan)


async def get_new_finished_matches(
    limit: int = 30,
    source: SourceName | None = None,
    dry_run: bool = False,
    include_filtered: bool = False,
    check_processed: bool = True,
    rejected_matches: list[MatchNormalized] | None = None,
    shadow_diagnostics: dict[str, int] | None = None,
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
    if used_source == "pandascore":
        try:
            comparison = await asyncio.wait_for(
                _run_liquipedia_shadow(
                    fetched,
                    fetch_limit,
                    require_fresh=require_fresh,
                ),
                timeout=source_config.LIQUIPEDIA_SHADOW_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            comparison = None
            logger.warning(
                "event=liquipedia_shadow_timed_out timeout_seconds=%s",
                source_config.LIQUIPEDIA_SHADOW_TIMEOUT_SECONDS,
            )
        if shadow_diagnostics is not None and comparison is not None:
            shadow_diagnostics.update(comparison)
        if selected_source == "auto":
            fetched = await _apply_liquipedia_final_card_selection(
                fetched,
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
