from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Literal

from . import config as source_config
from .filters import is_tier1_lan, is_valid_match
from .models import MatchNormalized, SourceUnavailableError
from .storage import channel_match_uid, is_processed, mark_channel_processed

SourceName = Literal["auto", "cs2api", "hltv"]

logger = logging.getLogger(__name__)


async def _fetch_from_source(source: Literal["cs2api", "hltv"], limit: int) -> list[MatchNormalized]:
    if source == "cs2api":
        from .sources import cs2api_source

        return await cs2api_source.fetch_finished_matches(limit=limit)
    from .sources import hltv_results_source

    return await hltv_results_source.fetch_finished_matches(limit=limit)


async def _choose_source(source: SourceName, limit: int) -> tuple[str, list[MatchNormalized]]:
    if source in {"cs2api", "hltv"}:
        matches = await _fetch_from_source(source, limit)
        logger.info("source=%s fetched=%s", source, len(matches))
        return source, matches

    try:
        matches = await _fetch_from_source("cs2api", limit)
        logger.info("source=cs2api fetched=%s", len(matches))
        if matches:
            return "cs2api", matches
        if not source_config.ENABLE_HLTV_FALLBACK:
            logger.warning("source=cs2api status=empty fallback=disabled")
            return "cs2api", []
        logger.warning("source=cs2api status=empty fallback=hltv")
    except SourceUnavailableError as exc:
        if not source_config.ENABLE_HLTV_FALLBACK:
            logger.warning("source=cs2api status=unavailable fallback=disabled error=%s", exc)
            raise
        logger.warning("source=cs2api status=unavailable fallback=hltv error=%s", exc)

    matches = await _fetch_from_source("hltv", limit)
    logger.info("source=hltv fetched=%s", len(matches))
    return "hltv", matches


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
) -> list[MatchNormalized]:
    selected_source = source or os.getenv("MATCH_SOURCE", "auto")
    if selected_source not in {"auto", "cs2api", "hltv"}:
        raise ValueError("--source must be auto, cs2api, or hltv")

    logger.info("match_fetcher start source=%s limit=%s dry_run=%s", selected_source, limit, dry_run)
    used_source, fetched = await _choose_source(selected_source, limit)
    filtered, valid_matches, tier1_lan_count = apply_quality_filters(fetched, include_filtered=include_filtered)

    new_matches: list[MatchNormalized] = []
    for match in filtered:
        if not match.is_tier1_lan and not include_filtered:
            continue
        if dry_run or not check_processed:
            new_matches.append(match)
            continue
        if await is_processed(match.match_uid):
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
            uid = channel_match_uid(match, channel_name)
            if not await is_processed(uid):
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
    parser.add_argument("--source", choices=["auto", "cs2api", "hltv"], default=os.getenv("MATCH_SOURCE", "auto"))
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
