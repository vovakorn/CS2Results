import pytest

from cs2bot import main, media_cards
from cs2bot.match_sources.models import MapResult, MatchNormalized, TournamentPlacement
from cs2bot.match_sources.storage import PendingDelivery


def unconfirmed_final():
    return MatchNormalized(
        source="liquipedia", match_id="unconfirmed", tournament_name="Test 2026",
        tournament_parent="Test/2026", team1_name="NAVI", team2_name="FaZe",
        score1=3, score2=1, is_final=True, winner_prize_usd=100,
        maps=[MapResult(name=str(i), score1=13, score2=9) for i in range(4)],
        tournament_placements=[TournamentPlacement(placement=str(i), team_name=f"Team {i}", prize_usd=100) for i in range(1, 5)],
    )


def test_unconfirmed_final_cannot_render_or_enqueue_standings_or_vrs(monkeypatch):
    match = unconfirmed_final()
    monkeypatch.setattr(main, "ENABLE_VRS", True)
    monkeypatch.setattr(main, "enqueue_result_delivery", lambda *a, **k: pytest.fail("unexpected enqueue"))
    assert not media_cards.can_render_final_card(match)
    assert not main._can_publish_tournament_standings(match)
    assert not main._enqueue_tournament_vrs(match, "global", "Global")
    assert media_cards.can_render_final_card(match.model_copy(update={"final_identity_confirmed": True}))


@pytest.mark.parametrize("platform", ["telegram", "instagram", "threads"])
@pytest.mark.parametrize("kind", ["tournament_standings", "tournament_vrs_standings"])
def test_legacy_unconfirmed_outbox_is_retained_without_publication(platform, kind, monkeypatch):
    deleted = []
    async def delete(item):
        deleted.append(item.key)
    monkeypatch.setattr(main, "delete_result_delivery", delete)
    monkeypatch.setattr(main, "can_render_tournament_vrs", lambda *args: True)
    monkeypatch.setattr(main, "claim_content_delivery", lambda *a: pytest.fail("unconfirmed VRS must not claim delivery"))
    pending = PendingDelivery(key="outbox/test", channel_id=platform, channel_name=platform,
                              match=unconfirmed_final(), created_at="2026-10-03T00:00:00Z", content_type=kind)
    suffix = "tournament_standings" if kind == "tournament_standings" else "tournament_vrs"
    if platform == "telegram":
        status = getattr(main, f"_deliver_{suffix}")(pending, {"chat_id": "test"}, "test")
    else:
        status = getattr(main, f"_deliver_{platform}_{suffix}")(pending, None)
    assert status == "failed"
    assert deleted == []
