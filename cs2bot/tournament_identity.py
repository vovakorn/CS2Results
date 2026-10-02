"""Exact reviewed event joins shared by preview, Threads and VRS jobs."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .tournament_preview import PreviewProfile, load_profiles


def event_key(profile: PreviewProfile) -> str:
    return f"event:{profile.key}"


@lru_cache(maxsize=8)
def _profiles(path: str, modified_ns: int) -> tuple[PreviewProfile, ...]:
    # Editorial approval/freshness gates belong to publication, not identity:
    # results and VRS still need this join after the event's review expires.
    return tuple(load_profiles(path))


def find_event(profiles_path, *, source: str, serie_id=None, tournament_id=None,
               liquipedia_page=None) -> PreviewProfile | None:
    path = Path(profiles_path)
    profiles = _profiles(str(path.resolve()), path.stat().st_mtime_ns)
    found = []
    for profile in profiles:
        if source == "liquipedia":
            matched = liquipedia_page == profile.liquipedia_page
        elif source == "pandascore":
            matched = (serie_id is not None and profile.pandascore_serie_id is not None
                       and str(serie_id) == str(profile.pandascore_serie_id)) or (
                tournament_id is not None and str(tournament_id) in {str(value) for value in profile.pandascore_tournament_ids})
        else:
            matched = False
        if matched:
            found.append(profile)
    if len(found) > 1:
        raise ValueError("conflicting reviewed tournament references")
    return found[0] if found else None


def event_for_match(match, profiles_path) -> PreviewProfile | None:
    refs = getattr(match, "source_refs", None)
    return find_event(profiles_path, source=getattr(match, "source", "pandascore"),
        serie_id=getattr(refs, "serie_id", None), tournament_id=getattr(refs, "tournament_id", None),
        liquipedia_page=getattr(match, "tournament_parent", None))
