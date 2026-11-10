"""Beijing's observed PandaScore identity joined to a complete LP test fixture.

PandaScore IDs/names/times are from the production dry-run on 9 November.
The four-team LP table below is synthetic; it is not a publication payload.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from cs2bot import main
from cs2bot.match_sources import match_fetcher
from cs2bot.match_sources.final_wait import FinalWait
from cs2bot.match_sources.models import (
    MapResult, MatchNormalized, SourceReferences, TournamentPlacement, VRSRankingSnapshot, VRSTeamSnapshot,
)
from cs2bot.match_sources.sources import liquipedia_source
from cs2bot.media_cards import can_render_final_card
from cs2bot.tournament_identity import event_for_match
from test_main import configured_runtime
from test_match_fetcher import final_wait_runtime


PAGE = "Intel_Extreme_Masters/2026/Beijing"


@pytest.fixture
def beijing_pair():
    primary = MatchNormalized(
        source="pandascore", match_id="1781486",
        tournament_name="IEM — Beijing — Playoffs", competition_key="IEM Beijing",
        source_refs=SourceReferences(serie_id="11136", tournament_id="22333"),
        tournament_tier="s", team1_name="TheMongolz", team2_name="MOUZ",
        team1_logo_url="https://example.org/mongolz.png", team2_logo_url="https://example.org/mouz.png",
        score1=0, score2=3, best_of=5, final_candidate_hint=True,
        start_date="2026-11-08T07:32:30Z", end_date="2026-11-08T10:08:47Z",
        date="2026-11-08T10:08:47Z",
    )
    final = MatchNormalized(
        source="liquipedia", match_id="beijing-test-final",
        tournament_name="Intel Extreme Masters Beijing 2026",
        competition_key="Intel Extreme Masters Beijing 2026", tournament_parent=PAGE,
        tournament_tier="s", is_lan=True, team1_name="MOUZ", team2_name="The MongolZ",
        score1=3, score2=0, best_of=5, date="2026-11-08 07:30:00",
        start_date="2026-11-08 07:30:00", winner_prize_usd=1000,
        maps=[MapResult(name=name, score1=13, score2=score)
              for name, score in [("Mirage", 5), ("Dust II", 6), ("Inferno", 4)]],
        tournament_placements_complete=True,
        tournament_placements=[TournamentPlacement(placement=str(i), team_name=name, prize_usd=1000 // i)
                               for i, name in enumerate(["MOUZ", "The MongolZ", "NAVI", "BetBoom Team"], 1)],
    )
    return primary, final


def test_beijing_provider_ids_resolve_after_preview_approval_expires(beijing_pair):
    for match in beijing_pair:
        profile = event_for_match(match, main.TOURNAMENT_PREVIEW_PROFILES_PATH)
        assert profile.key == "iem-beijing-2026"
        assert main._vrs_tournament_id(match) == "event:iem-beijing-2026"


def test_beijing_final_join_preserves_existing_delivery_identity_and_artwork(beijing_pair):
    primary, raw = beijing_pair
    final = match_fetcher.confirm_liquipedia_final_candidates([raw])[0][0]
    selected = match_fetcher._replace_with_liquipedia_finals([primary], [final])[0]
    assert can_render_final_card(selected)
    assert selected.match_uid == primary.match_uid == "match_v1_960cf13083e5db30a0a875dc"
    assert selected.editorial_admission_uid == primary.match_uid
    assert selected.team1_logo_url == primary.team2_logo_url
    assert selected.team2_logo_url == primary.team1_logo_url
    assert main._can_publish_tournament_standings(selected)
    diagnostic = main._match_diagnostic(selected)
    assert diagnostic["final_identity_confirmed"] is True
    assert diagnostic["map_count"] == 3
    assert diagnostic["placement_count"] == 4


@pytest.mark.parametrize("changes", [
    {"source_refs": SourceReferences(serie_id="99999", tournament_id="99999")},
    {"team1_name": "Another MongolZ"},
    {"start_date": "2026-11-07T07:30:00Z"},
])
def test_same_pair_does_not_join_a_different_event_or_match(beijing_pair, changes):
    primary, final = beijing_pair
    assert not match_fetcher._same_final_match(primary.model_copy(update=changes), final)


def test_reviewed_final_survives_absence_from_latest_liquipedia_page(monkeypatch, beijing_pair):
    primary, raw = beijing_pair
    calls = []

    async def scoped(limit, *, parent):
        calls.append(parent)
        return [raw]

    monkeypatch.setattr(liquipedia_source, "fetch_finished_matches", scoped)
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "test-only")
    selected = asyncio.run(match_fetcher._apply_liquipedia_final_card_selection(
        [primary], 90, False, dry_run=True, liquipedia_matches=[],
    ))
    assert calls == [PAGE]
    assert len(selected) == 1 and can_render_final_card(selected[0])


def test_complete_recent_final_avoids_another_scoped_request(monkeypatch, beijing_pair):
    primary, raw = beijing_pair
    monkeypatch.setattr(liquipedia_source, "fetch_finished_matches",
                        lambda *a, **k: pytest.fail("complete final was fetched again"))
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "test-only")
    selected = asyncio.run(match_fetcher._apply_liquipedia_final_card_selection(
        [primary], 90, False, dry_run=True, liquipedia_matches=[raw],
    ))
    assert can_render_final_card(selected[0])


def test_saved_final_wait_survives_both_providers_latest_pages(monkeypatch, beijing_pair, final_wait_runtime):
    primary, raw = beijing_pair
    waits, _ = final_wait_runtime
    waits[primary.match_uid] = FinalWait(match=primary, first_seen_at="2026-11-08T10:15:00Z")

    async def scoped(limit, *, parent):
        assert parent == PAGE
        return [raw]

    monkeypatch.setattr(liquipedia_source, "fetch_finished_matches", scoped)
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "test-only")
    selected = asyncio.run(match_fetcher._apply_liquipedia_final_card_selection(
        [], 90, True, dry_run=True, liquipedia_matches=[],
        now=datetime(2026, 11, 8, 10, 20, tzinfo=timezone.utc),
    ))
    assert len(selected) == 1 and can_render_final_card(selected[0])


def test_historical_recovery_enriches_the_final_without_global_latest_query(monkeypatch, beijing_pair):
    primary, raw = beijing_pair

    async def choose(*args, **kwargs):
        return "pandascore", [primary]

    async def scoped(limit, *, parent):
        assert parent == PAGE
        return [raw]

    monkeypatch.setattr(match_fetcher, "_choose_source", choose)
    monkeypatch.setattr(match_fetcher, "_fetch_from_source", lambda *a, **k: pytest.fail("global latest query"))
    monkeypatch.setattr(liquipedia_source, "fetch_finished_matches", scoped)
    monkeypatch.setattr(match_fetcher.source_config, "ENABLE_LIQUIPEDIA_FINAL_CARDS", True)
    monkeypatch.setattr(match_fetcher.source_config, "LIQUIPEDIA_API_KEY", "test-only")
    selected = asyncio.run(match_fetcher.get_new_finished_matches(
        source="pandascore", dry_run=True, liquipedia_parent=PAGE,
        start=datetime(2026, 11, 8, tzinfo=timezone.utc),
        end=datetime(2026, 11, 9, tzinfo=timezone.utc),
    ))
    assert len(selected) == 1 and can_render_final_card(selected[0])


def test_recovery_passes_exact_page_and_admits_only_beijing(monkeypatch, configured_runtime, beijing_pair):
    primary, _ = beijing_pair
    captured = {}

    async def fetch(**kwargs):
        captured.update(kwargs)
        return [primary.model_copy(update={"is_tier1_lan": True}),
                primary.model_copy(update={"source_refs": SourceReferences(serie_id="99999"), "is_tier1_lan": True})]

    monkeypatch.setattr(main, "get_new_finished_matches", fetch)
    monkeypatch.setattr(main, "_iter_channels", lambda: iter([{"id": "global", "name": "global", "chat_id": "test"}]))
    response = main.handler({"job": "tournament_recovery", "recovery_profile_key": "iem-beijing-2026",
                             "dry_run": True}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert captured["liquipedia_parent"] == PAGE
    assert body["filtered_skipped"] == 1
    assert body["per_channel"]["global"] == 1


def test_scoped_source_queries_parent_and_reads_table_without_stage_hint(monkeypatch):
    calls = []
    raw = {
        "pagename": PAGE, "parent": PAGE, "match2id": "test", "tournament": "Intel Extreme Masters Beijing 2026",
        "finished": "1", "date": "2026-11-08 07:30:00", "section": "Playoffs", "type": "offline",
        "match2opponents": [{"name": "MOUZ", "score": "3"}, {"name": "The MongolZ", "score": "0"}],
    }
    payouts = [{"placement": str(i), "opponentname": team, "prizemoney": "1000"}
               for i, team in enumerate(["MOUZ", "The MongolZ", "NAVI", "BetBoom Team"], 1)]

    class Response:
        status = 200

        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, *, params, **kwargs):
            calls.append(params)
            return Response({"result": payouts if url.endswith("/placement") else [raw]})

    async def read(response, *args):
        return json.dumps(response.data).encode()

    monkeypatch.setattr(liquipedia_source.source_config, "LIQUIPEDIA_API_KEY", "test-only")
    monkeypatch.setattr(liquipedia_source.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(liquipedia_source, "read_limited_response", read)
    matches = asyncio.run(liquipedia_source.fetch_finished_matches(100, parent=PAGE))
    assert calls[0]["conditions"] == f"[[finished::1]][[parent::{PAGE}]]"
    assert calls[1]["conditions"] == f"[[parent::{PAGE}]]"
    assert len(matches) == 1 and matches[0].tournament_placements_complete
    assert matches[0].final_candidate_hint is False
    assert len(match_fetcher.confirm_liquipedia_final_candidates(matches)[0]) == 1


def test_joined_beijing_final_queues_prizes_and_vrs_from_saved_event_baseline(monkeypatch, beijing_pair):
    primary, raw = beijing_pair
    final = match_fetcher._replace_with_liquipedia_finals(
        [primary], match_fetcher.confirm_liquipedia_final_candidates([raw])[0],
    )[0]
    queued, reads = [], []

    def snapshot(version, when, bonus):
        return VRSRankingSnapshot(
            source="Valve VRS", version=version, effective_at=when, fetched_at=when,
            teams=[VRSTeamSnapshot(team_id=str(i), team_name=p.team_name, rank=i,
                                   points=1000 - i * 100 + (bonus if i == 1 else 0))
                   for i, p in enumerate(final.tournament_placements, 1)],
        )

    async def read(*args):
        reads.append(args)
        return snapshot("before", "2026-10-31T10:00:00Z", 0)

    async def after():
        return snapshot("after", "2026-11-09T10:00:00Z", 100)

    async def write(*args, **kwargs):
        return True

    async def enqueue(match, channel, name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "publication_delivery_allowed", lambda *args: (True, 0))
    monkeypatch.setattr(main, "read_vrs_snapshot", read)
    monkeypatch.setattr(main, "fetch_vrs_snapshot", after)
    monkeypatch.setattr(main, "write_vrs_snapshot", write)
    monkeypatch.setattr(main, "enqueue_result_delivery", enqueue)
    assert main._enqueue_tournament_standings(final, "global", "global")
    assert [item["content_type"] for item in queued] == ["tournament_standings", "tournament_vrs_standings"]
    assert reads == [("event:iem-beijing-2026", "before", "initial")]
    assert queued[1]["vrs_impacts"][0].points_delta == 100
