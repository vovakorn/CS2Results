import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.models import (
    MapResult,
    MatchNormalized,
    SourceReferences,
    SourceUnavailableError,
    TournamentPlacement,
)
from cs2bot.media_cards import can_render_final_card, can_render_tournament_standings
from cs2bot.match_sources import storage


def _match(source="pandascore", match_id="1", tournament_name="IEM Cologne 2026"):
    return MatchNormalized(
        source=source,
        match_id=match_id,
        match_url=f"https://example.com/{match_id}",
        tournament_name=tournament_name,
        team1_name="NAVI",
        team2_name="FaZe",
        score1=2,
        score2=1,
        location="Cologne, Germany",
    )


def _incident_profile():
    parent = "StarLadder_StarSeries_Fall_2026"
    placements = [
        TournamentPlacement(placement="1", team_name="Vitality", prize_usd=500_000),
        TournamentPlacement(placement="2", team_name="Aurora Gaming", prize_usd=250_000),
        TournamentPlacement(placement="3", team_name="FURIA", prize_usd=125_000),
        TournamentPlacement(placement="4", team_name="MIBR", prize_usd=75_000),
        TournamentPlacement(placement="5", team_name="MOUZ", prize_usd=30_000),
        TournamentPlacement(placement="6", team_name="NRG", prize_usd=20_000),
    ]
    rows = [
        ("Aurora Gaming", "Vitality", 1, 3, "2026-09-20T21:00:00Z", "aurora-vitality-final"),
        ("Vitality", "FURIA", 2, 0, "2026-09-20T19:00:00Z", "vitality-furia"),
        ("FURIA", "MIBR", 2, 0, "2026-09-20T17:00:00Z", "furia-mibr"),
        ("Aurora Gaming", "Vitality", 2, 1, "2026-09-19T21:00:00Z", "aurora-vitality-earlier"),
        ("FURIA", "MOUZ", 2, 1, "2026-09-19T19:00:00Z", "furia-mouz"),
        ("NRG", "MIBR", 0, 2, "2026-09-19T17:00:00Z", "nrg-mibr"),
    ]
    matches = []
    for team1, team2, score1, score2, date, match_id in rows:
        matches.append(
            MatchNormalized(
                source="liquipedia",
                match_id=match_id,
                tournament_name="StarLadder StarSeries Fall 2026",
                competition_key="StarLadder StarSeries Fall 2026",
                tournament_parent=parent,
                tournament_placements_complete=True,
                tournament_section="Results",
                # Simulates the unreliable {1, 2} lookup hint on every match.
                final_candidate_hint=True,
                tournament_placements=placements,
                team1_name=team1,
                team2_name=team2,
                score1=score1,
                score2=score2,
                date=date,
                start_date=date,
                winner_prize_usd=500_000 if match_id == "aurora-vitality-final" else None,
                maps=(
                    [
                        MapResult(name="Mirage", score1=13, score2=9),
                        MapResult(name="Nuke", score1=10, score2=13),
                        MapResult(name="Ancient", score1=8, score2=13),
                    ]
                    if match_id == "aurora-vitality-final"
                    else []
                ),
            )
        )
    return matches


def _incident_pandascore_matches(liquipedia_matches):
    return [
        MatchNormalized(
            source="pandascore",
            match_id=f"ps-{match.match_id}",
            tournament_name="StarLadder — Fall 2026 — Playoffs",
            competition_key="StarLadder Fall 2026",
            competition_key_aliases=["Fall 2026"],
            team1_name=match.team1_name,
            team2_name=match.team2_name,
            score1=match.score1,
            score2=match.score2,
            date=match.date,
            start_date=match.start_date,
        )
        for match in liquipedia_matches
    ]


def test_final_dry_run_never_writes_pending_state(monkeypatch):
    profile = _incident_profile()
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "configured")

    async def fetch(*args):
        return profile

    async def unexpected_write(*args, **kwargs):
        pytest.fail("dry-run must not mutate durable pending state")

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fetch)
    for name in ("clear_pending_liquipedia_final", "get_or_create_pending_liquipedia_final", "mark_pending_liquipedia_final_ready"):
        monkeypatch.setattr(match_fetcher, name, unexpected_write)
    selected = asyncio.run(match_fetcher._apply_liquipedia_final_card_selection(
        _incident_pandascore_matches(profile), 100, False, dry_run=True,
    ))
    assert selected[0].source == "liquipedia"
    assert asyncio.run(match_fetcher._apply_liquipedia_final_card_selection([], 100, False, dry_run=True)) == []


def test_final_does_not_replace_the_same_teams_at_another_event():
    final = match_fetcher.confirm_liquipedia_final_candidates(_incident_profile())[0][0]
    unrelated = _incident_pandascore_matches([final])[0].model_copy(update={
        "competition_key": "Another Fall 2026", "tournament_name": "Another Fall 2026",
    })
    assert match_fetcher._replace_with_liquipedia_finals([unrelated], [final]) == [unrelated]


def test_shared_first_place_cannot_confirm_a_grand_final():
    profile = _incident_profile()
    for match in profile:
        match.tournament_placements[0].placement = "1–2"
    confirmed, reasons = match_fetcher.confirm_liquipedia_final_candidates(profile)
    assert confirmed == []
    assert reasons[profile[0].match_id] == "incomplete_standings"


class _MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None):
        self.objects[Key] = Body
        return {"ETag": '"memory"'}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"ETag": '"memory"', "Metadata": {}}


def test_auto_uses_pandascore_when_it_returns_matches(monkeypatch):
    async def fake_fetch(source, limit):
        assert source == "pandascore"
        return [_match()]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "pandascore"


def test_final_card_selection_replaces_primary_with_complete_liquipedia_final():
    primary = _match()
    primary.team1_logo_url = "https://cdn.pandascore.co/images/team/image/1/navi.png"
    primary.team2_logo_url = "https://cdn.pandascore.co/images/team/image/2/faze.png"
    primary.date = "2026-08-23T16:00:00Z"
    primary.source_refs = SourceReferences(tournament_id="pandascore-tournament-42")
    final = _match(source="liquipedia", match_id="liquipedia-final")
    final.date = primary.date
    final.is_final = True
    final.final_identity_confirmed = True
    final.winner_prize_usd = 500_000
    final.maps = [
        MapResult(name="Mirage", score1=13, score2=9),
        MapResult(name="Nuke", score1=11, score2=13),
        MapResult(name="Ancient", score1=13, score2=10),
    ]

    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])

    assert selected[0].source == "liquipedia"
    assert selected[0].match_id == final.match_id
    assert selected[0].match_uid == primary.match_uid
    assert selected[0].vrs_baseline_id == "pandascore-tournament-42"
    assert selected[0].team1_logo_url == primary.team1_logo_url
    assert selected[0].team2_logo_url == primary.team2_logo_url


def test_final_card_selection_maps_logos_by_team_when_providers_reverse_sides():
    primary = _match()
    primary.team1_logo_url = "https://cdn.pandascore.co/images/team/image/1/navi.png"
    primary.team2_logo_url = "https://cdn.pandascore.co/images/team/image/2/faze.png"
    primary.date = "2026-08-23T16:00:00Z"
    final = _match(source="liquipedia", match_id="liquipedia-final")
    final.team1_name, final.team2_name = "FaZe", "NAVI"
    final.date = primary.date
    final.is_final = True
    final.final_identity_confirmed = True
    final.winner_prize_usd = 500_000
    final.maps = [
        MapResult(name="Mirage", score1=13, score2=9),
        MapResult(name="Nuke", score1=11, score2=13),
        MapResult(name="Ancient", score1=13, score2=10),
    ]

    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])

    assert selected[0].team1_logo_url == primary.team2_logo_url
    assert selected[0].team2_logo_url == primary.team1_logo_url


def test_final_card_selection_keeps_primary_when_liquipedia_final_is_incomplete():
    primary = _match()
    primary.date = "2026-08-23T16:00:00Z"
    final = _match(source="liquipedia", match_id="liquipedia-final")
    final.date = primary.date
    final.is_final = True
    final.winner_prize_usd = 500_000

    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])

    assert selected == [primary]


def test_final_card_selection_uses_liquipedia_when_complete_standings_are_available():
    primary = _match()
    primary.date = "2026-08-23T16:00:00Z"
    final = _match(source="liquipedia", match_id="liquipedia-final")
    final.date = primary.date
    final.is_final = True
    final.final_identity_confirmed = True
    final.tournament_parent = "IEM/Cologne/2026"
    final.tournament_placements = [
        TournamentPlacement(placement="1", team_name="NAVI", prize_usd=400_000),
        TournamentPlacement(placement="2", team_name="FaZe", prize_usd=180_000),
    ]

    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])
    assert selected[0].source == "liquipedia"
    assert selected[0].final_identity_confirmed is True


def test_starladder_incident_profile_confirms_only_the_actual_final(caplog):
    caplog.set_level(logging.INFO)
    liquipedia_matches = _incident_profile()
    confirmed, rejected = match_fetcher.confirm_liquipedia_final_candidates(liquipedia_matches)

    assert [match.match_id for match in confirmed] == ["aurora-vitality-final"]
    assert len(rejected) == 5
    pandascore_matches = _incident_pandascore_matches(liquipedia_matches)
    selected = match_fetcher._replace_with_liquipedia_finals(pandascore_matches, confirmed)

    assert len(selected) == 6
    assert sum(match.source == "liquipedia" for match in selected) == 1
    assert sum(match.source == "pandascore" for match in selected) == 5
    final = next(match for match in selected if match.source == "liquipedia")
    assert final.team1_name == "Aurora Gaming"
    assert final.team2_name == "Vitality"
    assert (final.score1, final.score2) == (1, 3)
    assert final.final_identity_confirmed is True
    assert liquipedia_matches[0].match_uid in final.canonical_match_uid_candidates
    assert can_render_final_card(final) is True
    assert can_render_tournament_standings(final.tournament_placements) is True
    assert len(final.tournament_placements) == 6
    assert rejected["aurora-vitality-earlier"] == "winner_not_first_place"
    assert "event=liquipedia_final_identity_confirmed" in caplog.text
    assert "reason=winner_not_first_place" in caplog.text


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ("incomplete_table", "incomplete_standings"),
        ("winner", "winner_not_first_place"),
        ("not_latest", "not_latest_in_tournament_parent"),
    ],
)
def test_liquipedia_final_requires_complete_winner_and_latest_evidence(change, expected_reason):
    profile = _incident_profile()
    final = profile[0]
    if change == "incomplete_table":
        for match in profile:
            match.tournament_placements = match.tournament_placements[:2]
    elif change == "winner":
        final.score1, final.score2 = 3, 1
    else:
        profile.append(
            final.model_copy(
                update={
                    "match_id": "later-competitive-match",
                    "team1_name": "FURIA",
                    "team2_name": "MIBR",
                    "score1": 2,
                    "score2": 1,
                    "date": "2026-09-20T22:00:00Z",
                    "start_date": "2026-09-20T22:00:00Z",
                    "is_final": False,
                    "final_identity_confirmed": False,
                }
            )
        )

    confirmed, rejected = match_fetcher.confirm_liquipedia_final_candidates(profile)

    assert not any(match.match_id == "aurora-vitality-final" for match in confirmed)
    assert rejected["aurora-vitality-final"] == expected_reason


def test_liquipedia_first_final_waits_then_bridge_suppresses_late_pandascore_duplicate(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    profile = _incident_profile()
    final = profile[0]
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "configured")

    async def fetch(source, limit):
        assert source == "liquipedia"
        return profile

    pending_state = {}

    async def get_or_create(match, *, now=None):
        bridge_uid = match.final_bridge_uid
        pending = pending_state.get(bridge_uid)
        if pending is None:
            pending = SimpleNamespace(
                key=f"pending/{bridge_uid}",
                bridge_uid=bridge_uid,
                match=match,
                created_at=now.isoformat().replace("+00:00", "Z"),
                status="waiting",
                created=True,
            )
            pending_state[bridge_uid] = pending
        else:
            pending.created = False
        return pending

    async def mark_ready(pending):
        pending.status = "ready"
        return pending

    async def clear_pending(bridge_uid):
        pending_state.pop(bridge_uid, None)

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fetch)
    monkeypatch.setattr(match_fetcher, "get_or_create_pending_liquipedia_final", get_or_create)
    monkeypatch.setattr(match_fetcher, "mark_pending_liquipedia_final_ready", mark_ready)
    monkeypatch.setattr(match_fetcher, "clear_pending_liquipedia_final", clear_pending)

    seen_at = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)
    early = asyncio.run(
        match_fetcher._apply_liquipedia_final_card_selection([], 100, False, now=seen_at)
    )
    assert early == []

    due = asyncio.run(
        match_fetcher._apply_liquipedia_final_card_selection(
            [], 100, False, now=seen_at + timedelta(minutes=11)
        )
    )
    assert len(due) == 1
    assert due[0].source == "liquipedia"

    s3 = _MemoryS3()
    published = []
    asyncio.run(storage.mark_channel_processed(due[0], "global", client=s3, bucket="bucket"))
    published.append(due[0].match_uid)

    still_due = asyncio.run(
        match_fetcher._apply_liquipedia_final_card_selection(
            [], 100, False, now=seen_at + timedelta(minutes=12)
        )
    )
    if still_due and not asyncio.run(
        storage.is_channel_processed(still_due[0], "global", client=s3, bucket="bucket")
    ):
        asyncio.run(storage.mark_channel_processed(still_due[0], "global", client=s3, bucket="bucket"))
        published.append(still_due[0].match_uid)

    late_pandascore = _incident_pandascore_matches([final])[0].model_copy(
        update={
            "competition_key": "StarLadder Fall 2026",
            "tournament_name": "StarLadder — Fall 2026 — Playoffs",
        }
    )
    replaced = asyncio.run(
        match_fetcher._apply_liquipedia_final_card_selection(
            [late_pandascore], 100, False, now=seen_at + timedelta(minutes=13)
        )
    )
    assert replaced[0].source == "liquipedia"
    assert replaced[0].match_uid == late_pandascore.match_uid
    assert asyncio.run(
        storage.is_channel_processed(replaced[0], "global", client=s3, bucket="bucket")
    ) is True
    assert len(published) == 1
    assert "event=liquipedia_final_pending_created" in caplog.text
    assert "resolution=wait_elapsed" in caplog.text


def test_liquipedia_shadow_timeout_does_not_block_primary_results(monkeypatch, caplog):
    async def fake_fetch(source, limit):
        assert source == "pandascore"
        return [_match()]

    async def slow_shadow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"matched": 1}

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    monkeypatch.setattr(match_fetcher, "_run_liquipedia_shadow", slow_shadow)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_SHADOW_TIMEOUT_SECONDS", 0.001)

    with caplog.at_level(logging.WARNING):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(source="pandascore", dry_run=True)
        )

    assert matches[0].source == "pandascore"
    assert "event=liquipedia_shadow_timed_out" in caplog.text


def test_auto_falls_back_to_liquipedia_when_pandascore_empty(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "pandascore":
            return []
        return [_match(source="liquipedia", match_id="2")]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "liquipedia"


def test_auto_falls_back_to_liquipedia_when_pandascore_unavailable(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "pandascore":
            raise SourceUnavailableError("down")
        return [_match(source="liquipedia", match_id="2")]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))
    assert matches[0].source == "liquipedia"


def test_auto_does_not_fall_back_to_liquipedia_when_fallback_disabled(monkeypatch):
    calls = []

    async def fake_fetch(source, limit):
        calls.append(source)
        return []

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    matches = asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))

    assert matches == []
    assert calls == ["pandascore"]


def test_auto_raises_when_pandascore_unavailable_and_fallback_disabled(monkeypatch):
    async def fake_fetch(source, limit):
        raise SourceUnavailableError("down")

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", False)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with pytest.raises(SourceUnavailableError):
        asyncio.run(match_fetcher.get_new_finished_matches(source="auto", dry_run=True))


def test_auto_falls_back_when_primary_is_stale_in_production(monkeypatch):
    calls = []
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        calls.append(source)
        match = _match(source=source, match_id="1" if source == "pandascore" else "2")
        match.end_date = "2000-01-01T00:00:00Z" if source == "pandascore" else recent
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    used_source, matches = asyncio.run(
        match_fetcher._choose_source("auto", 10, require_fresh=True)
    )

    assert used_source == "liquipedia"
    assert matches[0].source == "liquipedia"
    assert calls == ["pandascore", "liquipedia"]


def test_auto_never_returns_stale_primary_when_fallback_fails(monkeypatch):
    async def fake_fetch(source, limit):
        if source == "liquipedia":
            raise SourceUnavailableError("blocked")
        match = _match()
        match.end_date = "2000-01-01T00:00:00Z"
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with pytest.raises(SourceUnavailableError, match="no usable match source"):
        asyncio.run(match_fetcher._choose_source("auto", 10, require_fresh=True))


def test_auto_falls_back_when_primary_has_no_valid_matches(monkeypatch):
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        match = _match(source=source, match_id="1" if source == "pandascore" else "2")
        match.end_date = recent
        if source == "pandascore":
            match.score1 = None
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FALLBACK", True)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    used_source, _ = asyncio.run(
        match_fetcher._choose_source("auto", 10, require_fresh=True)
    )
    assert used_source == "liquipedia"


def test_production_drops_stale_matches_even_when_source_has_recent_data(monkeypatch):
    stale_tier1 = _match(match_id="old")
    stale_tier1.end_date = "2000-01-01T00:00:00Z"
    recent_non_tier1 = _match(match_id="new", tournament_name="Regional Finals")
    recent_non_tier1.end_date = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        return [stale_tier1, recent_non_tier1]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(
            source="pandascore",
            dry_run=False,
            check_processed=False,
        )
    )

    assert matches == []


def test_include_filtered_returns_filtered_reason(monkeypatch):
    async def fake_fetch(source, limit):
        return [_match(source="liquipedia", tournament_name="Regional Finals")]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(source="liquipedia", dry_run=True, include_filtered=True)
    )
    assert matches[0].is_tier1_lan is False
    assert matches[0].filter_reason == "not_tier1"


def test_pandascore_tier_is_logged_as_shadow_diagnostic(caplog):
    selected = _match(source="pandascore", tournament_name="IEM Cologne 2026")
    selected.tournament_tier = "s"
    rejected = _match(source="pandascore", tournament_name="Regional Finals")
    rejected.tournament_tier = "a"

    with caplog.at_level(logging.INFO):
        output, valid, selected_count = match_fetcher.apply_quality_filters(
            [selected, rejected]
        )

    assert output == [selected]
    assert valid == [selected, rejected]
    assert selected_count == 1
    assert "event=pandascore_tier_diagnostics" in caplog.text
    assert "s_selected=1" in caplog.text
    assert "a_rejected=1" in caplog.text


def test_liquipedia_shadow_comparison_aligns_reversed_team_order():
    primary = _match()
    primary.date = "2026-08-09T12:00:00Z"
    primary.tournament_tier = "s"

    shadow = _match(source="liquipedia", match_id="lp-1")
    shadow.date = "2026-08-09T11:55:00Z"
    shadow.team1_name = "FaZe Clan"
    shadow.team2_name = "Natus Vincere"
    shadow.score1 = 1
    shadow.score2 = 2
    shadow.tournament_tier = "a"
    shadow.maps = [MapResult(name="Mirage", score1=13, score2=9)]
    shadow.forfeit = True

    comparison = match_fetcher.compare_source_matches([primary], [shadow])

    assert comparison == {
        "primary_count": 1,
        "liquipedia_count": 1,
        "matched": 1,
        "primary_only": 0,
        "liquipedia_only": 0,
        "score_mismatches": 0,
        "best_of_mismatches": 0,
        "tier_mismatches": 1,
        "liquipedia_map_coverage": 1,
        "liquipedia_technical_results": 1,
    }


def test_liquipedia_shadow_comparison_uses_aliases_and_start_time():
    primary = _match()
    primary.team1_name = "1WIN"
    primary.team2_name = "Liquid"
    primary.start_date = "2026-08-08T23:45:00Z"
    primary.end_date = "2026-08-09T00:50:00Z"
    primary.date = primary.end_date

    shadow = _match(source="liquipedia", match_id="lp-1")
    shadow.team1_name = "1w Team"
    shadow.team2_name = "Team Liquid"
    shadow.start_date = "2026-08-08 23:45:00"
    shadow.date = shadow.start_date
    shadow.score1 = 2
    shadow.score2 = 1

    comparison = match_fetcher.compare_source_matches([primary], [shadow])

    assert comparison["matched"] == 1
    assert comparison["primary_only"] == 0
    assert comparison["liquipedia_only"] == 0
    assert comparison["score_mismatches"] == 0


def test_liquipedia_shadow_logs_comparison_without_changing_primary(monkeypatch, caplog):
    calls = []
    recent = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        calls.append(source)
        match = _match(source=source, match_id="1" if source == "pandascore" else "lp-1")
        match.date = recent
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_SHADOW", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "liquipedia-key")
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    diagnostics = {}

    with caplog.at_level(logging.INFO):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(
                source="auto",
                dry_run=True,
                shadow_diagnostics=diagnostics,
            )
        )

    assert matches[0].source == "pandascore"
    assert calls == ["pandascore", "liquipedia"]
    assert "event=liquipedia_shadow_comparison" in caplog.text
    assert "matched=1" in caplog.text
    assert diagnostics["matched"] == 1
    assert diagnostics["score_mismatches"] == 0


def test_liquipedia_shadow_failure_never_blocks_primary(monkeypatch, caplog):
    async def fake_fetch(source, limit):
        if source == "liquipedia":
            raise SourceUnavailableError("down")
        match = _match()
        match.date = datetime.now(timezone.utc).isoformat()
        return [match]

    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_SHADOW", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "liquipedia-key")
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)

    with caplog.at_level(logging.WARNING):
        matches = asyncio.run(
            match_fetcher.get_new_finished_matches(source="auto", dry_run=True)
        )

    assert matches[0].source == "pandascore"
    assert "event=liquipedia_shadow_failed" in caplog.text


def test_configured_online_tier1_stage_enters_public_output(monkeypatch):
    selected = _match(
        source="pandascore",
        tournament_name="BLAST Bounty — 2026 Season 2 — Online Stage",
    )
    selected.location = None
    selected.end_date = datetime.now(timezone.utc).isoformat()

    async def fake_fetch(source, limit):
        return [selected]

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fake_fetch)
    diagnostics = []

    matches = asyncio.run(
        match_fetcher.get_new_finished_matches(
            source="pandascore",
            dry_run=False,
            check_processed=False,
            rejected_matches=diagnostics,
        )
    )

    assert matches == [selected]
    assert selected.is_tier1_lan is True
    assert selected.filter_reason is None
    assert diagnostics == []


def test_log_source_freshness_warns_when_latest_match_is_stale(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    match = _match()
    match.end_date = "2026-05-24T10:00:00Z"

    with caplog.at_level(logging.WARNING):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "pandascore",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is False
    assert age_hours == 96
    assert "event=source_stale" in caplog.text


def test_log_source_freshness_accepts_recent_match(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_STALENESS_HOURS", 48)
    match = _match()
    match.end_date = "2026-05-28T09:00:00Z"

    with caplog.at_level(logging.INFO):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "pandascore",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is True
    assert age_hours == 1
    assert "event=source_fresh" in caplog.text


def test_log_source_freshness_rejects_future_timestamp(monkeypatch, caplog):
    monkeypatch.setattr(match_fetcher.source_config, "MAX_SOURCE_FUTURE_SKEW_HOURS", 6)
    match = _match()
    match.end_date = "2026-05-29T10:00:00Z"

    with caplog.at_level(logging.WARNING):
        fresh, age_hours = match_fetcher.log_source_freshness(
            "pandascore",
            [match],
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
        )

    assert fresh is False
    assert age_hours == -24
    assert "event=source_future_timestamp" in caplog.text


def test_freshness_uses_latest_of_end_date_date_and_start_date():
    match = _match()
    match.end_date = "2026-05-24T10:00:00Z"
    match.date = "2026-05-28T09:00:00Z"
    match.start_date = "2026-05-27T10:00:00Z"
    reference = datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc)

    assert match_fetcher.latest_match_datetime([match]) == datetime(
        2026, 5, 28, 9, 0, tzinfo=timezone.utc
    )
    assert match_fetcher.is_match_fresh(match, now=reference) is True
