"""Regression for the real Thunderpick source-switch / filter / outbox path."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from cs2bot import main
from cs2bot.match_sources import match_fetcher, storage
from cs2bot.match_sources.filters import is_tier1_lan
from cs2bot.match_sources.models import MapResult, MatchNormalized, SourceReferences, TournamentPlacement
from cs2bot.media_cards import can_render_final_card
from test_main import configured_runtime
from test_storage import FakeS3


@pytest.fixture
def final_pipeline(monkeypatch, configured_runtime):
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(hours=6), now - timedelta(hours=1)
    primary = MatchNormalized(
        source="pandascore", match_id="1741367",
        tournament_name="Thunderpick World Championship — 2026 — Playoffs",
        competition_key="Thunderpick World Championship 2026", tournament_tier="a",
        source_refs=SourceReferences(tournament_id="22133", serie_id="11069"),
        team1_name="FURIA", team2_name="BetBoom Team", score1=3, score2=2,
        best_of=5, final_candidate_hint=True,
        start_date=start.isoformat(), end_date=end.isoformat(), date=end.isoformat(),
    )
    final = MatchNormalized(
        source="liquipedia", match_id="oyckLEjvba_R02-M001",
        tournament_name="Thunderpick World Championship 2026",
        competition_key="Thunderpick World Championship 2026",
        tournament_tier="s", publisher_tier="Tier 1", is_lan=True,
        tournament_parent="Thunderpick_World_Championship/2026",
        tournament_placements_complete=True, final_candidate_hint=True,
        team1_name="BetBoom Team", team2_name="FURIA", score1=2, score2=3,
        best_of=5, date=start.isoformat(), start_date=start.isoformat(),
        # Test payouts are fixtures, not assertions about the real prize table.
        tournament_placements=[
            TournamentPlacement(placement=str(i), team_name=name, prize_usd=prize)
            for i, (name, prize) in enumerate([
                ("FURIA", 500000), ("BetBoom Team", 100000),
                ("Aurora Gaming", 60000), ("Team Falcons", 30000),
            ], 1)
        ],
        winner_prize_usd=500000,
        maps=[MapResult(name=name, score1=a, score2=b) for name, a, b in [
            ("Mirage", 13, 8), ("Nuke", 8, 13), ("Dust2", 13, 9),
            ("Ancient", 9, 13), ("Train", 10, 13),
        ]],
    )
    s3 = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda: s3)
    monkeypatch.setattr(storage, "_bucket", lambda: "test-bucket")
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "configured")
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_SHADOW", False)

    async def fetch(source, limit):
        return [(primary if source == "pandascore" else final).model_copy(deep=True)]

    async def held_claim(*args, **kwargs):
        return None  # Stop before an external send, after exercising real enqueue.

    def forbidden_request(*args, **kwargs):
        pytest.fail("final pipeline tests must not contact external services")

    monkeypatch.setattr(match_fetcher, "_fetch_from_source", fetch)
    monkeypatch.setattr(main, "CHANNELS", [{"id": "global", "name": "global", "chat_id": "@test"}])
    monkeypatch.setattr(main, "instagram_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "threads_publishing_enabled", lambda: False)
    monkeypatch.setattr(main, "ENABLE_VRS", False)
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    monkeypatch.setattr(main, "_delivery_is_current", lambda *args: True)
    monkeypatch.setattr(main, "claim_channel_delivery", held_claim)
    monkeypatch.setattr(main, "_record_post_analytics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_notify_admin", lambda *args: None)
    for name in ("enqueue_result_delivery", "list_pending_result_deliveries", "is_channel_processed"):
        monkeypatch.setattr(main, name, getattr(storage, name))
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_request)
    return primary, final, s3


def test_thunderpick_auto_keeps_admission_through_final_swap_and_real_outbox(final_pipeline):
    primary, raw_final, s3 = final_pipeline
    assert is_tier1_lan(primary) == (True, None)
    assert is_tier1_lan(raw_final) == (False, "not_tier1")

    response = main.handler({"job": "results", "source": "auto"}, None)
    assert response["statusCode"] == 200
    pending = asyncio.run(storage.list_pending_result_deliveries())
    assert len(pending) == 1
    final = pending[0].match
    assert final.source == "liquipedia"
    assert final.is_tier1_lan is True
    assert final.filter_reason is None
    assert final.match_uid == primary.match_uid
    assert final.vrs_baseline_id == "22133"
    assert can_render_final_card(final)
    assert final.score1 == raw_final.score1 and final.maps == raw_final.maps
    assert final.tournament_placements == raw_final.tournament_placements
    assert final.tournament_tier == raw_final.tournament_tier
    assert final.source_refs is raw_final.source_refs is None
    for key, value in s3.objects.items():
        if key.startswith("outbox/results/"):
            assert "editorial_admission_uid" not in json.loads(value["Body"])["match"]
    assert all(not key.startswith("processed/") for key in s3.objects)

    # Repeated invocations use the same durable outbox key, not another result.
    main.handler({"job": "results", "source": "auto"}, None)
    assert len(asyncio.run(storage.list_pending_result_deliveries())) == 1
    asyncio.run(storage.mark_channel_processed(final, "global"))
    assert asyncio.run(storage.is_channel_processed(primary, "global"))
    assert asyncio.run(storage.is_channel_processed(final, "global"))


@pytest.mark.parametrize("changes", [
    {"tournament_tier": "b"},
    {"tournament_tier": None},
    {"tournament_name": "Thunderpick World Championship — Closed Qualifier"},
    {"tournament_name": "Thunderpick World Championship — Showmatch"},
    {"is_lan": False},
    {"location": "Online"},
    {"score1": 1, "score2": 1},
])
def test_rejected_primary_cannot_grant_editorial_admission(final_pipeline, changes):
    primary, raw_final, _ = final_pipeline
    primary = primary.model_copy(update=changes)
    final = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])
    assert selected[0].source == "liquipedia"
    assert selected[0].editorial_admission_uid is None
    assert match_fetcher.apply_quality_filters(selected)[0] == []


@pytest.mark.parametrize("changes,reason", [
    ({"is_lan": False}, "explicitly_not_lan"),
    ({"location": "Online"}, "not_tier1"),
    ({"tournament_name": "Thunderpick World Championship — Online Stage"}, "not_tier1"),
    ({"location": "Online", "is_lan": None}, "online_location"),
    ({"tournament_name": "Thunderpick World Championship — Online Stage", "is_lan": None}, "online_tournament"),
    ({"tournament_name": "Thunderpick World Championship — Qualifier"}, "excluded_tournament"),
    ({"tournament_name": "Thunderpick World Championship — Showmatch"}, "excluded_tournament"),
    ({"team1_name": "BetBoom Academy"}, "excluded_team"),
    ({"team1_name": "BetBoom Junior"}, "excluded_team"),
    ({"team1_name": "BetBoom Youth"}, "excluded_team"),
])
def test_inherited_admission_does_not_override_final_hard_exclusions(final_pipeline, changes, reason):
    primary, raw_final, _ = final_pipeline
    confirmed = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    final = match_fetcher._replace_with_liquipedia_finals([primary], [confirmed])[0]
    assert final.editorial_admission_uid == primary.match_uid
    assert is_tier1_lan(final.model_copy(update=changes)) == (False, reason)


@pytest.mark.parametrize("changes", [
    {"editorial_admission_uid": None},
    {"editorial_admission_uid": "match_v1_another_match"},
    {"canonical_uid_override": None},
    {"canonical_uid_override": "match_v1_another_match"},
    {"is_final": False},
    {"final_identity_confirmed": False},
])
def test_partial_or_mismatched_final_proof_cannot_admit_unlisted_event(final_pipeline, changes):
    primary, raw_final, _ = final_pipeline
    confirmed = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    final = match_fetcher._replace_with_liquipedia_finals([primary], [confirmed])[0]
    assert is_tier1_lan(final.model_copy(update=changes)) == (False, "not_tier1")


def test_same_teams_at_another_event_cannot_inherit_admission(final_pipeline):
    primary, raw_final, _ = final_pipeline
    final = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    final = final.model_copy(update={
        "competition_key": "Another Championship 2026", "tournament_name": "Another Championship 2026"})
    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])
    assert selected == [primary]
    assert selected[0].source == "pandascore"


def test_raw_or_legacy_liquipedia_final_has_no_blanket_tier_admission(final_pipeline):
    primary, raw_final, _ = final_pipeline
    confirmed = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    legacy = confirmed.model_copy(update={"canonical_uid_override": primary.match_uid, "is_tier1_lan": True})
    assert is_tier1_lan(legacy) == (False, "not_tier1")
    assert is_tier1_lan(confirmed) == (False, "not_tier1")


def test_inherited_admission_cannot_bypass_score_validation(final_pipeline):
    primary, raw_final, _ = final_pipeline
    confirmed = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    final = match_fetcher._replace_with_liquipedia_finals([primary], [confirmed])[0]
    final.score1 = final.score2 = 1
    assert match_fetcher.apply_quality_filters([final])[0] == []


def test_checked_replacement_without_venue_keeps_primary_admission(final_pipeline):
    primary, raw_final, _ = final_pipeline
    raw_final.is_lan = None
    confirmed = match_fetcher.confirm_liquipedia_final_candidates([raw_final])[0][0]
    final = match_fetcher._replace_with_liquipedia_finals([primary], [confirmed])[0]
    output, _, count = match_fetcher.apply_quality_filters([final])
    assert output == [final] and count == 1
    assert final.is_tier1_lan is True
