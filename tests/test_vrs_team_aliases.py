"""Exact Valve/Liquipedia name joins must preserve source and roster identity."""
import pytest

from cs2bot.match_sources.models import TournamentPlacement
from cs2bot.match_sources.vrs import VRSDataError, calculate_impacts, normalize_snapshot

PAIRS = [("BetBoom Team", "BETBOOM"), ("Aurora Gaming", "Aurora"),
         ("Team Falcons", "Falcons"), ("9z Team", "9z")]
SOURCE = "Valve VRS live/global"


def snapshot(name, *, after=False, source=SOURCE, team_id="stable-roster", extra=()):
    return normalize_snapshot({"version": "after" if after else "before",
        "effective_at": "2026-10-20T00:00:00Z" if after else "2026-10-05T00:00:00Z",
        "teams": [{"team_id": team_id, "team_name": name, "name": name,
                   "points": 1100 if after else 1000, "rank": 9 if after else 11}, *extra]},
        source=source, fetched_at="2026-10-21T00:00:00Z")


def placement(name):
    return [TournamentPlacement(placement="1", team_name=name, prize_usd=1000)]


@pytest.mark.parametrize("lp,valve", PAIRS)
def test_exact_valve_alias_preserves_liquipedia_label_and_source_data(lp, valve):
    before, after = snapshot(valve), snapshot(valve, after=True)
    original = before.model_dump()
    item, = calculate_impacts(placement(lp), before, after)
    assert (item.team_name, item.team_id) == (lp, "stable-roster")
    assert (item.before_points, item.after_points, item.points_delta) == (1000, 1100, 100)
    assert (item.before_rank, item.after_rank, item.rank_delta) == (11, 9, 2)
    assert before.model_dump() == original


@pytest.mark.parametrize("lp,valve", PAIRS)
@pytest.mark.parametrize("collision_in_after", [False, True])
def test_alias_collision_never_selects_one_of_two_rosters(lp, valve, collision_in_after):
    extra = ({"team_id": "second-roster", "name": lp, "points": 900, "rank": 20},)
    before = snapshot(valve, extra=() if collision_in_after else extra)
    after = snapshot(valve, after=True, extra=extra if collision_in_after else ())
    with pytest.raises(VRSDataError, match="ambiguous"):
        calculate_impacts(placement(lp), before, after)


@pytest.mark.parametrize("lp,valve", PAIRS)
def test_alias_does_not_bridge_different_source_team_ids(lp, valve):
    with pytest.raises(VRSDataError, match="incomplete"):
        calculate_impacts(placement(lp), snapshot(valve),
                          snapshot(valve, after=True, team_id="different-roster"))


@pytest.mark.parametrize("lp,valve", PAIRS)
def test_alias_does_not_match_academy_or_name_substrings(lp, valve):
    with pytest.raises(VRSDataError, match="incomplete"):
        calculate_impacts(placement(lp+" Academy"), snapshot(valve), snapshot(valve, after=True))


@pytest.mark.parametrize("lp,valve", PAIRS)
def test_alias_is_scoped_to_official_valve_source(lp, valve):
    with pytest.raises(VRSDataError, match="incomplete"):
        calculate_impacts(placement(lp), snapshot(valve, source="different-provider"),
                          snapshot(valve, after=True, source="different-provider"))
