import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2bot import main
from cs2bot.match_sources.models import MatchNormalized, SourceReferences, TournamentPlacement, VRSRankingSnapshot, VRSTeamSnapshot
from cs2bot.match_sources.vrs import VRSDataError
from cs2bot.tournament_identity import event_for_match, event_key
from cs2bot.tournament_preview import PreviewProfile, load_profiles


@pytest.fixture
def event(tmp_path, monkeypatch):
    stages = json.loads((Path(__file__).parent / "fixtures/tournament_preview_demo.json").read_text())["stages"]
    profile = PreviewProfile(key="test-2026", liquipedia_page="Test/2026", pandascore_serie_id=7,
        pandascore_tournament_ids=[41, 42], approved=True, verified_at="2026-09-01T00:00:00Z",
        story="История турнира из проверенного источника.", story_source_url="https://example.org", stages=stages)
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([profile.model_dump(mode="json")]))
    monkeypatch.setattr(main, "TOURNAMENT_PREVIEW_PROFILES_PATH", str(path))
    return profile, path


def match(source="pandascore", stage="41"):
    return MatchNormalized(source=source, match_id="one", tournament_name="Test 2026", team1_name="NAVI",
        team2_name="FaZe", score1=3, score2=1, best_of=5, is_final=True, end_date="2026-10-08T20:00:00Z",
        source_refs=SourceReferences(serie_id="7", tournament_id=stage) if source == "pandascore" else None,
        tournament_parent="Test/2026" if source == "liquipedia" else None,
        tournament_placements=[TournamentPlacement(placement="1", team_name="NAVI"), TournamentPlacement(placement="2", team_name="FaZe")])


def snapshot(version="first", effective="2026-10-01T00:00:00Z", fetched="2026-10-01T10:00:00Z"):
    return VRSRankingSnapshot(source="Valve VRS", version=version, effective_at=effective, fetched_at=fetched,
        teams=[VRSTeamSnapshot(team_id="navi", team_name="NAVI", points=100, rank=2),
               VRSTeamSnapshot(team_id="faze", team_name="FaZe", points=110, rank=1)])


def test_all_stages_and_liquipedia_final_share_threads_and_vrs_identity(event):
    profile, path = event
    for item in [match(stage="41"), match(stage="42"), match("liquipedia").model_copy(update={"vrs_baseline_id": "42"})]:
        assert event_key(event_for_match(item, path)) == "event:test-2026"
        assert main._threads_tournament_key(item) == "threads:event:test-2026"
        assert main._vrs_tournament_id(item) == "event:test-2026"
    # A stage without serie metadata and a manual radar still use reviewed IDs.
    assert main._threads_tournament_key(match().model_copy(update={"source_refs": SourceReferences(tournament_id="42")})) == "threads:event:test-2026"
    assert main._threads_tournament_key_from_id("42") == "threads:event:test-2026"
    assert len(main._group_threads_content("schedule", [match(stage="41"), match(stage="42")], "2026-10-01")) == 1


def test_matching_name_without_provider_identity_never_joins(event):
    _, path = event
    unrelated = match().model_copy(update={"source_refs": SourceReferences(serie_id="8", tournament_id="43")})
    assert event_for_match(unrelated, path) is None
    assert main._threads_tournament_key(unrelated) == "threads:pandascore:43"
    assert event_for_match(match("liquipedia").model_copy(update={"tournament_parent": "Test/2025"}), path) is None


def test_historical_identity_survives_expired_or_revoked_editorial_approval(event):
    profile, path = event
    path.write_text(json.dumps([profile.model_copy(update={"approved": False}).model_dump(mode="json")]))
    assert main._threads_tournament_key(match()) == "threads:event:test-2026"


def test_registry_rejects_overlapping_provider_ids(event):
    profile, path = event
    other = profile.model_copy(update={"key": "other", "liquipedia_page": "Other/2026", "pandascore_serie_id": 8})
    path.write_text(json.dumps([profile.model_dump(mode="json"), other.model_dump(mode="json")]))
    with pytest.raises(ValueError, match="ambiguous PandaScore stage"):
        load_profiles(path)


def test_preview_baseline_is_captured_once_and_reported_from_storage(event, monkeypatch):
    profile, _ = event
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    saved, calls = {}, []
    async def read(key, phase, version):
        calls.append(("read", key, phase, version))
        return saved.get(key)
    async def fetch():
        calls.append(("fetch",))
        return snapshot()
    async def write(key, phase, value, **options):
        assert phase == "before" and options == {"initial": True}
        calls.append(("write",))
        saved[key] = value
        return True
    monkeypatch.setattr(main, "read_vrs_snapshot", read)
    monkeypatch.setattr(main, "fetch_vrs_snapshot", fetch)
    monkeypatch.setattr(main, "write_vrs_snapshot", write)
    p = SimpleNamespace(start=date(2026, 10, 2))
    assert main._capture_preview_vrs_baseline(profile, p)["status"] == "saved"
    assert main._capture_preview_vrs_baseline(profile, p)["status"] == "existing"
    assert calls.count(("fetch",)) == calls.count(("write",)) == 1
    assert saved["event:test-2026"].version == "first"


@pytest.mark.parametrize("field", ["effective_at", "fetched_at"])
def test_snapshot_dated_after_start_cannot_become_baseline(event, monkeypatch, field):
    profile, _ = event
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    async def read(*args):
        return None
    async def fetch():
        return snapshot().model_copy(update={field: "2026-10-03T00:00:00Z"})
    monkeypatch.setattr(main, "read_vrs_snapshot", read)
    monkeypatch.setattr(main, "fetch_vrs_snapshot", fetch)
    monkeypatch.setattr(main, "write_vrs_snapshot", lambda *args, **kwargs: pytest.fail("late baseline write"))
    with pytest.raises(VRSDataError, match="before the tournament"):
        main._capture_preview_vrs_baseline(profile, SimpleNamespace(start=date(2026, 10, 2)))


def test_liquipedia_final_calculates_vrs_from_preview_baseline(event, monkeypatch):
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    before = snapshot()
    after = snapshot("second", "2026-10-09T00:00:00Z", "2026-10-09T10:00:00Z")
    after.teams[0].points = 140
    after.teams[1].points = 90
    reads, queued = [], []
    async def read(*args):
        reads.append(args)
        return before
    async def fetch():
        return after
    async def write(*args, **kwargs):
        return True
    async def enqueue(*args, **kwargs):
        queued.append(kwargs)
        return True
    monkeypatch.setattr(main, "read_vrs_snapshot", read)
    monkeypatch.setattr(main, "read_latest_vrs_snapshot", lambda *args: pytest.fail("wrong baseline lookup"))
    monkeypatch.setattr(main, "fetch_vrs_snapshot", fetch)
    monkeypatch.setattr(main, "write_vrs_snapshot", write)
    monkeypatch.setattr(main, "enqueue_result_delivery", enqueue)
    assert main._enqueue_tournament_vrs(match("liquipedia"), "threads", "threads")
    assert reads == [("event:test-2026", "before", "initial")]
    assert [item.points_delta for item in queued[0]["vrs_impacts"]] == [40, -20]
