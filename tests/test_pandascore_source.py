import asyncio
import logging
from datetime import datetime, timezone

import pytest

from cs2bot.match_sources.filters import is_tier1_lan
from cs2bot.match_sources.models import SourceUnavailableError
from cs2bot.match_sources.sources import pandascore_source


def _sample_match():
    return {
        "id": 12345,
        "status": "finished",
        "number_of_games": 3,
        "begin_at": "2026-07-28T10:00:00Z",
        "end_at": "2026-07-28T12:10:00Z",
        "league": {"id": 1, "name": "IEM"},
        "serie": {
            "id": 2,
            "full_name": "IEM Cologne 2026",
            "image_url": "https://cdn.pandascore.co/images/serie/image/2/iem-cologne.png",
        },
        "tournament": {"id": 3, "name": "Playoffs", "tier": "S"},
        "opponents": [
            {
                "opponent": {
                    "id": 10,
                    "name": "NAVI",
                    "image_url": "https://cdn.pandascore.co/images/team/image/10/navi.png",
                }
            },
            {
                "opponent": {
                    "id": 20,
                    "name": "FaZe",
                    "image_url": "https://cdn.pandascore.co/images/team/image/20/faze.png",
                }
            },
        ],
        "results": [
            {"team_id": 20, "score": 1},
            {"team_id": 10, "score": 2},
        ],
        "winner_id": 10,
        "rescheduled": True,
        "original_scheduled_at": "2026-07-28T09:00:00Z",
        "forfeit": False,
    }


def _sample_upcoming():
    item = _sample_match()
    item["id"] = 67890
    item["status"] = "not_started"
    item["scheduled_at"] = "2026-07-30T11:00:00Z"
    item["end_at"] = None
    item["results"] = []
    item.pop("winner_id")
    return item


def test_pandascore_normalizes_series_result_by_team_id():
    matches = pandascore_source._normalize_raw_matches([_sample_match()])

    assert len(matches) == 1
    match = matches[0]
    assert match.source == "pandascore"
    assert match.match_id == "12345"
    assert match.team1_name == "NAVI"
    assert match.team2_name == "FaZe"
    assert match.team1_logo_url.endswith("/10/navi.png")
    assert match.team2_logo_url.endswith("/20/faze.png")
    assert (match.score1, match.score2) == (2, 1)
    assert match.status == "finished"
    assert match.best_of == 3
    assert match.tournament_name == "IEM — IEM Cologne 2026 — Playoffs"
    assert match.competition_key == "IEM Cologne 2026"
    assert match.tournament_logo_url.endswith("/2/iem-cologne.png")
    assert match.source_refs.model_dump() == {
        "league_id": "1",
        "serie_id": "2",
        "tournament_id": "3",
        "team1_id": "10",
        "team2_id": "20",
        "winner_team_id": "10",
    }
    assert match.tournament_tier == "s"
    assert match.start_date == "2026-07-28T10:00:00Z"
    assert match.end_date == "2026-07-28T12:10:00Z"
    assert match.original_scheduled_at == "2026-07-28T09:00:00Z"
    assert match.rescheduled is True
    assert match.forfeit is False
    assert match.maps == []


def test_pandascore_blast_bounty_finals_are_recognized_as_tier1_lan():
    item = _sample_match()
    item["league"] = {"id": 11, "name": "BLAST Bounty"}
    item["serie"] = {"id": 12, "full_name": "2026 Season 2 Finals"}
    item["tournament"] = {"id": 13, "name": "Playoffs"}
    item["opponents"] = [
        {"opponent": {"id": 10, "name": "3DMAX"}},
        {"opponent": {"id": 20, "name": "MOUZ"}},
    ]
    item["results"] = [
        {"team_id": 10, "score": 1},
        {"team_id": 20, "score": 2},
    ]

    match = pandascore_source._normalize_raw_matches([item])[0]

    assert match.tournament_name == "BLAST Bounty — 2026 Season 2 Finals — Playoffs"
    assert match.location is None
    assert match.is_lan is None
    assert is_tier1_lan(match) == (True, None)


def test_pandascore_blast_bounty_online_stage_is_selected():
    item = _sample_match()
    item["league"] = {"id": 11, "name": "BLAST Bounty"}
    item["serie"] = {"id": 12, "full_name": "2026 Season 2"}
    item["tournament"] = {"id": 13, "name": "Online Stage"}

    match = pandascore_source._normalize_raw_matches([item])[0]

    assert is_tier1_lan(match) == (True, None)


def test_pandascore_skips_incomplete_match_without_two_opponents():
    item = _sample_match()
    item["opponents"] = item["opponents"][:1]

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_skips_match_not_confirmed_finished():
    item = _sample_match()
    item["status"] = "running"

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_skips_unknown_best_of():
    item = _sample_match()
    item["number_of_games"] = 2

    assert pandascore_source._normalize_raw_matches([item]) == []


def test_pandascore_recent_range_avoids_undated_records(monkeypatch):
    monkeypatch.setattr(pandascore_source.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    monkeypatch.setattr(pandascore_source.source_config, "MAX_SOURCE_FUTURE_SKEW_HOURS", 6)

    result = pandascore_source._recent_match_range(
        datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    )

    assert result == "2026-07-22T14:00:00Z,2026-07-29T20:00:00Z"


def test_pandascore_requires_token(monkeypatch):
    monkeypatch.setattr(pandascore_source.source_config, "PANDASCORE_API_TOKEN", None)

    with pytest.raises(SourceUnavailableError, match="credentials"):
        asyncio.run(pandascore_source.fetch_finished_matches())


def test_pandascore_normalizes_featured_upcoming_match():
    matches = pandascore_source._normalize_raw_upcoming([_sample_upcoming()])

    assert len(matches) == 1
    match = matches[0]
    assert match.match_id == "67890"
    assert match.scheduled_at == "2026-07-30T11:00:00Z"
    assert match.team1_logo_url.endswith("/10/navi.png")
    assert match.team2_logo_url.endswith("/20/faze.png")
    assert match.tournament_logo_url.endswith("/2/iem-cologne.png")
    assert match.source_refs.team1_id == "10"
    assert match.source_refs.team2_id == "20"
    assert match.source_refs.winner_team_id is None
    assert match.tournament_tier == "s"
    assert match.original_scheduled_at == "2026-07-28T09:00:00Z"
    assert match.rescheduled is True
    assert match.forfeit is False
    assert match.is_featured is True
    assert match.feature_reason == "tier1_tournament"


def test_pandascore_keeps_dark_mode_team_logo_as_fallback():
    item = _sample_upcoming()
    item["opponents"][0]["opponent"]["dark_mode_image_url"] = (
        "https://cdn.pandascore.co/images/team/image/10/navi-dark.png"
    )

    match = pandascore_source._normalize_raw_upcoming([item])[0]

    assert match.team1_logo_url.endswith("/10/navi.png")
    assert match.team1_logo_fallback_url.endswith("/10/navi-dark.png")


def test_pandascore_uses_default_logo_without_dark_mode_variant():
    item = _sample_upcoming()

    match = pandascore_source._normalize_raw_upcoming([item])[0]

    assert match.team1_logo_url.endswith("/10/navi.png")
    assert match.team1_logo_fallback_url is None


def test_pandascore_upcoming_rejects_running_match():
    item = _sample_upcoming()
    item["status"] = "running"

    assert pandascore_source._normalize_raw_upcoming([item]) == []


def test_pandascore_utc_range_normalizes_timezone():
    result = pandascore_source._utc_range(
        datetime.fromisoformat("2026-07-30T00:00:00+03:00"),
        datetime.fromisoformat("2026-07-31T00:00:00+03:00"),
    )

    assert result == "2026-07-29T21:00:00Z,2026-07-30T21:00:00Z"


def test_pandascore_enriches_missing_tiers_in_one_batched_request(monkeypatch):
    first_item = _sample_match()
    first_item["tournament"].pop("tier")
    second_item = _sample_match()
    second_item["id"] = 12346
    second_item["tournament"] = {"id": 4, "name": "Group Stage"}
    matches = pandascore_source._normalize_raw_matches([first_item, second_item])
    calls = []

    async def fake_fetch(path, params):
        calls.append((path, params))
        return [{"id": 3, "tier": "A"}, {"id": 4, "tier": "B"}]

    pandascore_source._tournament_tier_cache.clear()
    monkeypatch.setattr(pandascore_source, "_fetch_json", fake_fetch)

    asyncio.run(pandascore_source._enrich_tournament_tiers(matches))

    assert calls == [
        (
            "/tournaments",
            {"filter[id]": "3,4", "per_page": 2},
        )
    ]
    assert [match.tournament_tier for match in matches] == ["a", "b"]

    asyncio.run(pandascore_source._enrich_tournament_tiers(matches))
    assert len(calls) == 1


def test_pandascore_tier_enrichment_failure_keeps_matches(monkeypatch, caplog):
    item = _sample_match()
    item["tournament"].pop("tier")
    matches = pandascore_source._normalize_raw_matches([item])

    async def fake_fetch(path, params):
        raise SourceUnavailableError("temporary tournament error")

    pandascore_source._tournament_tier_cache.clear()
    monkeypatch.setattr(pandascore_source, "_fetch_json", fake_fetch)

    with caplog.at_level(logging.WARNING):
        asyncio.run(pandascore_source._enrich_tournament_tiers(matches))

    assert matches[0].tournament_tier is None
    assert "event=pandascore_tier_enrichment_failed" in caplog.text
