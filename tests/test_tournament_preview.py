import asyncio
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from cs2bot import media_cards, tournament_preview_job as job
from cs2bot.match_sources.sources import tournament_preview_source as source
from cs2bot.tournament_preview import (
    PreviewBranding, PreviewProfile, PreviewTeam, PreviewUnavailable, TournamentPreview,
    check_profile, date_range, format_preview_caption, format_preview_instagram_caption,
    format_preview_threads_caption, publication_window,
)
from cs2bot.tournament_preview_cards import render_preview_cards

NOW = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/tournament_preview_demo.json"


def preview(count=0):
    data = json.loads(FIXTURE.read_text())
    data.update(participant_count=count or None,
                teams=[{"team_id": str(i), "name": f"Team {i}"} for i in range(count)])
    return TournamentPreview.model_validate(data)


def profile(**updates):
    data = {"key": "test-2026", "liquipedia_page": "Test/2026", "approved": True,
            "verified_at": NOW, "story": "Проверенная история прошлого розыгрыша.",
            "story_source_url": "https://example.org/history",
            "stages": json.loads(FIXTURE.read_text())["stages"]}
    return PreviewProfile.model_validate({**data, **updates})


def passport(**updates):
    return {"result": [{"pagename": "Test/2026", "name": "Test 2026",
                        "startdate": "2026-10-02", "enddate": "2026-10-08", "prizepool": "1000000",
                        "locations": {"country1": "dk", "city1": "Copenhagen", "venue1": "Arena"},
                        "participantsnumber": 4, **updates}]}


@pytest.mark.parametrize("days,allowed", [(0, False), (1, True), (2, True), (3, False), (-1, False)])
def test_publication_window_uses_moscow_days(days, allowed):
    moment = datetime(2026, 10, 2, tzinfo=timezone.utc) - timedelta(days=days)
    assert publication_window(preview(), moment) is allowed


@pytest.mark.parametrize("start,end,year,expected", [
    ("2026-10-03", "2026-10-11", False, "3–11 октября"),
    ("2026-10-03", "2026-10-11", True, "3–11 октября 2026"),
    ("2026-11-11", "2026-11-15", False, "11–15 ноября"),
    ("2026-10-31", "2026-11-02", False, "31 октября – 2 ноября"),
    ("2026-10-03", "2026-10-03", False, "3 октября"),
    ("2026-12-30", "2027-01-02", False, "30 декабря 2026 – 2 января 2027"),
])
def test_dates_are_readable_and_cross_year_ranges_remain_unambiguous(start, end, year, expected):
    assert date_range(date.fromisoformat(start), date.fromisoformat(end), year=year) == expected


def test_three_stage_cover_fits_a_date_range_crossing_months():
    data = preview().model_dump(mode="json")
    data["start"] = "2026-09-28"
    data["stages"][0].update(start="2026-09-28", end="2026-09-30")
    middle = {**data["stages"][0], "kind": "swiss", "start": "2026-09-30", "end": "2026-10-03"}
    data["stages"][-1]["start"] = "2026-10-04"
    data["stages"].insert(1, middle)
    p = TournamentPreview.model_validate(data)
    assert Image.open(io.BytesIO(render_preview_cards(p)[0])).size == (1080, 1080)


def test_unapproved_stale_and_future_reviews_are_blocked():
    for p in [profile(approved=False), profile(verified_at=NOW - timedelta(days=8)),
              profile(verified_at=NOW + timedelta(seconds=1))]:
        with pytest.raises(PreviewUnavailable):
            check_profile(p, NOW)


def test_profile_rejects_query_injection_and_story_without_source():
    with pytest.raises(ValidationError):
        profile(liquipedia_page="Test]] OR [[finished::1")
    with pytest.raises(ValidationError):
        profile(story_source_url=None)


def test_branding_requires_sources_and_only_bundled_logo_names():
    for data in [{"logo_asset": "esl-pro-league.png"}, {"headline": "Большая арена"},
                 {"logo_asset": "../secrets.png", "logo_source_url": "https://example.org/logo"},
                 {"accent": "not-a-color"}]:
        with pytest.raises(ValidationError):
            PreviewBranding.model_validate(data)


def test_initial_stage_and_non_overlapping_playoffs_are_required():
    stages = profile().model_dump(mode="json")["stages"]
    with pytest.raises(ValidationError):
        profile(stages=[stages[-1]])
    stages[-1]["start"] = str(stages[0]["start"])
    with pytest.raises(ValidationError):
        profile(stages=stages)


def test_reviewed_stages_cannot_silently_lag_changed_api_dates():
    data = preview().model_dump()
    data["start"] -= timedelta(days=1)
    with pytest.raises(ValidationError):
        TournamentPreview.model_validate(data)


@pytest.mark.parametrize("change", [{"prizepool": None}, {"prizepool": 1.5}, {"prizepool": True},
                                    {"prizepool": "NaN"}, {"locations": {}}, {"startdate": None},
                                    {"locations": {"country1": "dk"}}, {"pagename": "Wrong/2026"}])
def test_missing_or_ambiguous_passport_blocks_preview(monkeypatch, change):
    async def fetched(*args):
        return passport(**change)
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    with pytest.raises(PreviewUnavailable):
        asyncio.run(source.fetch_tournament_preview(profile(), now=NOW))


def test_partial_participants_are_omitted_without_losing_passport(monkeypatch):
    async def fetched(*args):
        return passport()
    async def participants(*args):
        return [PreviewTeam(team_id="1", name="Team 1")], datetime(2026, 10, 2, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    monkeypatch.setattr(source, "_participants", participants)
    result = asyncio.run(source.fetch_tournament_preview(profile(pandascore_serie_id=7), now=NOW))
    assert result.teams == []
    assert result.participant_count == 4
    assert result.warnings == ["participants_incomplete"]


def test_previous_champion_story_does_not_invent_title_defence(monkeypatch):
    async def fetched(path, params):
        return passport(previous="Test/2025") if path == "/tournament" else {
            "result": [{"placement": "1", "opponentname": "Old champion"}]}
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    result = asyncio.run(source.fetch_tournament_preview(profile(story=None, story_source_url=None), now=NOW))
    assert result.story == "Предыдущий розыгрыш выиграла команда Old champion."
    assert "защищать" not in result.story


def test_participants_join_all_series_stages_dedupes_and_reads_first_match(monkeypatch):
    paths = []
    async def fetched(path, params):
        paths.append(path)
        if path.endswith("/tournaments"):
            return [{"id": 1}, {"id": 2}]
        if path.endswith("/rosters"):
            return [{"team": {"id": 8, "name": "Shared"}}, {"team": {"id": int(path.split('/')[2]), "name": path}}]
        return [{"scheduled_at": "2026-10-02T10:00:00+03:00"}]
    monkeypatch.setattr(source.pandascore_source, "_fetch_json", fetched)
    teams, first = asyncio.run(source._participants(77))
    assert len(teams) == 3
    assert "/tournaments/1/rosters" in paths and "/tournaments/2/rosters" in paths
    assert first.hour == 10


def test_live_roster_schema_does_not_count_players_as_teams(monkeypatch):
    async def fetched(path, params):
        if path.endswith("/tournaments"):
            return [{"id": 1}]
        if path.endswith("/rosters"):
            return {"type": "Team", "rosters": [{"id": 8, "name": "Shared",
                     "players": [{"id": 99, "name": "Player must not become a team"}]}]}
        return []
    monkeypatch.setattr(source.pandascore_source, "_fetch_json", fetched)
    teams, first = asyncio.run(source._participants(77))
    assert [team.name for team in teams] == ["Shared"]
    assert first is None


def test_caption_contains_required_facts_and_escapes_editorial_html():
    p = preview().model_copy(update={"story": "История <script> & текст"})
    caption = format_preview_caption(p)
    assert "&lt;script&gt; &amp;" in caption
    for text in ["Призовой фонд", "📍 Копенгаген", "Формат:\nГруппы", "Плей-офф", "финал BO5", "МСК", "Источники"]:
        assert text in caption
    assert len(caption) <= 1024
    assert "Место:" not in caption and "Место проведения" not in caption


def test_prize_money_is_separated_from_club_payments_and_matches_api_total():
    data = preview().model_dump()
    data.update(prize_money_usd=300000, club_reward_usd=700000,
                prize_source_url="https://example.org/rules")
    caption = format_preview_caption(TournamentPreview.model_validate(data))
    assert "Призовые и выплаты: $1 000 000" in caption
    assert "Игрокам $300 000; клубам $700 000" in caption
    data["club_reward_usd"] = 600000
    with pytest.raises(ValidationError):
        TournamentPreview.model_validate(data)


def test_blast_participation_fees_are_not_labeled_as_player_prizes():
    data = preview().model_dump()
    data.update(prize_money_usd=350000, club_reward_usd=650000,
                prize_split_kind="prize_participation", prize_source_url="https://example.org/rules")
    p = TournamentPreview.model_validate(data)
    caption = format_preview_caption(p)
    assert "Призовые $350 000; за участие $650 000" in caption
    assert "Игрокам" not in caption
    assert Image.open(io.BytesIO(render_preview_cards(p)[0])).size == (1080, 1080)


def test_draft_is_explicit_keeps_review_age_limit_and_default_fetch_is_blocked(monkeypatch):
    calls = []
    async def fetched(*args):
        calls.append("API")
        return passport()
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    p = profile(approved=False)
    with pytest.raises(PreviewUnavailable, match="profile_not_approved"):
        asyncio.run(source.fetch_tournament_preview(p, now=NOW))
    assert calls == []
    result = asyncio.run(source.fetch_tournament_preview(p, now=NOW, allow_draft=True))
    assert result.warnings == ["editorial_draft"]
    assert format_preview_caption(result, draft=True).startswith("<b>Черновик")
    with pytest.raises(PreviewUnavailable, match="editorial_review_stale"):
        check_profile(profile(approved=False, verified_at=NOW-timedelta(days=8)), NOW, allow_draft=True)


def test_missing_event_logo_uses_full_title_space_without_losing_passport(monkeypatch, tmp_path):
    from cs2bot import tournament_preview_cards as cards
    p = preview()
    p.branding = PreviewBranding(logo_asset="missing.png", logo_source_url="https://example.org/logo")
    original = format_preview_caption(p)
    monkeypatch.setattr(media_cards, "ASSET_DIR", tmp_path)
    boxes = []
    original_text = cards._text
    def tracked(draw, text, box, **kwargs):
        if text == p.name.upper():
            boxes.append(box)
        return original_text(draw, text, box, **kwargs)
    monkeypatch.setattr(cards, "_text", tracked)
    assert cards._event_logo(p) is None
    assert Image.open(io.BytesIO(render_preview_cards(p)[0])).size == (1080, 1080)
    assert boxes[0][2] == 1010
    assert format_preview_caption(p) == original


def test_future_examples_are_not_in_the_publishable_registry_and_have_bundled_logos():
    from cs2bot.tournament_preview import load_profiles
    root = Path(__file__).parents[1]
    examples = load_profiles(root / "data/tournament_preview_examples.json")
    active = load_profiles(root / "data/tournament_preview_profiles.json")
    assert {p.key for p in examples}.isdisjoint({p.key for p in active})
    for p in examples:
        assert not p.approved
        with pytest.raises(PreviewUnavailable, match="profile_not_approved"):
            check_profile(p, p.verified_at)
        with Image.open(media_cards.ASSET_DIR / "tournament-preview" / p.branding.logo_asset) as image:
            assert image.format == "PNG"


def test_playoffs_venue_is_not_assigned_to_the_whole_event(monkeypatch):
    async def fetched(*args):
        return passport()
    monkeypatch.setattr(source, "_liquipedia_json", fetched)
    result = asyncio.run(source.fetch_tournament_preview(profile(venue_scope="playoffs"), now=NOW))
    assert result.locations == ["Копенгаген, Дания, Arena (плей-офф)"]


@pytest.mark.parametrize("count", [0, 1, 4, 10, 16, 24, 128])
def test_card_variants_are_square_and_include_every_participant(count):
    p = preview(count)
    cards = render_preview_cards(p, demo=True)
    assert len(cards) == 1 + (count + 15) // 16
    assert len(cards) <= 10
    for card in cards:
        image = Image.open(io.BytesIO(card))
        assert image.size == (1080, 1080)


def test_long_names_and_contrasting_logos_fit(monkeypatch):
    p = preview(10)
    p.name = "INTERNATIONAL COUNTER-STRIKE CHAMPIONSHIP — EXTREMELY LONG TOURNAMENT NAME 2026"
    p.teams[0].name = "International Counter-Strike Championship Division Team"
    def logo(url, **kwargs):
        return Image.new("RGBA", (80, 80), "black" if url.endswith("black") else "white")
    monkeypatch.setattr(media_cards, "fetch_team_logo", logo)
    p.teams[0].logo_url = "https://cdn.pandascore.co/black"
    p.teams[1].logo_url = "https://cdn.pandascore.co/white"
    assert len(render_preview_cards(p)) == 2


def test_branding_and_transition_summaries_preserve_full_passport():
    from cs2bot.tournament_preview import load_profiles
    p = preview()
    reviewed = load_profiles(Path(__file__).parents[1] / "data/tournament_preview_profiles.json")[0]
    p.branding = reviewed.branding
    p.stages[0].card_summary = "GSL · 16 → 6"
    p.stages[1].card_summary = "BO3 · финал BO5"
    original = format_preview_caption(p)
    assert Image.open(io.BytesIO(render_preview_cards(p, demo=True)[0])).size == (1080, 1080)
    assert format_preview_caption(p) == original


def runtime(tmp_path, monkeypatch, *, enabled=True, uncertain=False, marker_failure=False):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([profile().model_dump(mode="json")]))
    calls = []
    async def fetched(*args, **kwargs):
        calls.append("fetch")
        return preview()
    monkeypatch.setattr(job, "fetch_tournament_preview", fetched)
    monkeypatch.setattr(job, "render_preview_cards", lambda *args: [b"png"])
    class Uncertain(Exception):
        pass
    async def reconcile(*args):
        calls.append("reconcile")
        return False
    async def claim(*args):
        calls.append("claim")
        return "claim"
    async def state(*args):
        calls.append("state")
        return "claim"
    async def marker(*args):
        calls.append("marker")
        if marker_failure:
            raise RuntimeError("storage")
    async def release(*args):
        calls.append("release")
    def send(*args, **kwargs):
        calls.append("send")
        if uncertain:
            raise Uncertain()
    result = SimpleNamespace(ENABLE_TOURNAMENT_PREVIEWS=enabled, TELEGRAM_MEDIA_CARDS=True,
        instagram_publishing_enabled=lambda: False, threads_publishing_enabled=lambda: False,
        TOURNAMENT_PREVIEW_PROFILES_PATH=path, _iter_channels=lambda: [{"id": "c", "chat_id": "chat"}],
        _error_response=lambda status, code: {"statusCode": status, "body": json.dumps({"error": code})},
        reconcile_content_delivery=reconcile, claim_content_delivery=claim,
        mark_delivery_claim_attempting=state, mark_delivery_claim_sent=state,
        mark_content_processed=marker, release_delivery_claim=release, send_photo_to_telegram=send,
        _record_post_analytics=lambda *args, **kwargs: None, TelegramDeliveryUncertainError=Uncertain,
        _retain_uncertain_delivery_claim=lambda *args: calls.append("retain"),
        _notify_admin=lambda *args: calls.append("alert"))
    return result, calls


def test_disabled_job_never_fetches(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch, enabled=False)
    assert json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])["skipped_reason"] == "disabled"
    assert calls == []


def test_dry_run_has_no_delivery_or_storage_effects(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch)
    body = json.loads(job.run_preview_job("test-2026", True, r, now=NOW)["body"])
    assert calls == ["fetch"]
    assert body["previews"][0]["eligible"]


def test_uncertain_delivery_keeps_claim_and_never_retries(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch, uncertain=True)
    assert job.run_preview_job("test-2026", False, r, now=NOW)["statusCode"] == 502
    assert calls.count("send") == 1
    assert "retain" in calls and "release" not in calls and "marker" not in calls


def test_marker_failure_after_send_does_not_release_claim(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch, marker_failure=True)
    job.run_preview_job("test-2026", False, r, now=NOW)
    assert "send" in calls and "marker" in calls and "release" not in calls


def test_duplicate_claim_prevents_send(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch)
    async def no_claim(*args):
        return None
    r.claim_content_delivery = no_claim
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    assert body["duplicates_skipped"] == 1
    assert "send" not in calls


@pytest.mark.parametrize("card_count,method", [(0, "text"), (1, "photo"), (2, "album")])
def test_confirmed_preview_delivery_preserves_media_and_finishes_marker(tmp_path, monkeypatch, card_count, method):
    r, calls = runtime(tmp_path, monkeypatch)
    r.TELEGRAM_MEDIA_CARDS = bool(card_count)
    monkeypatch.setattr(job, "render_preview_cards", lambda *args: [b"png"] * card_count)
    sent = []
    def send(kind, *args, **kwargs):
        sent.append((kind, args, kwargs))
    r.send_to_telegram = lambda *args, **kwargs: send("text", *args, **kwargs)
    r.send_photo_to_telegram = lambda *args, **kwargs: send("photo", *args, **kwargs)
    r.send_media_group_to_telegram = lambda *args, **kwargs: send("album", *args, **kwargs)
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    assert body["messages_sent"] == 1 and body["delivery_failures"] == 0
    assert len(sent) == 1 and sent[0][0] == method
    if method == "album":
        assert len(sent[0][1][1]) == 2 and len(sent[0][2]["filenames"]) == 2
    assert "marker" in calls and "release" not in calls


def test_late_invocation_cannot_publish_a_preview(tmp_path, monkeypatch):
    r, calls = runtime(tmp_path, monkeypatch)
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW + timedelta(days=1))["body"])
    assert body["previews"][0]["skipped_reason"] == "outside_publication_window"
    assert "claim" not in calls and "send" not in calls


def test_main_route_disabled_preview_needs_no_live_credentials(monkeypatch):
    from cs2bot import main
    monkeypatch.setattr(main, "ENABLE_TOURNAMENT_PREVIEWS", False)
    response = main.handler({"job": "tournament_preview", "preview_key": "test-2026"}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["skipped_reason"] == "disabled"


def test_social_captions_preserve_sources_and_required_facts():
    p = preview().model_copy(update={"short_story": "Проверенная история прошлого розыгрыша."})
    instagram = format_preview_instagram_caption(p)
    assert "<b>" not in instagram and '<a href=' not in instagram
    assert p.passport_source_url in instagram and p.story_source_url in instagram
    assert all(stage.format_text in instagram for stage in p.stages)
    threads = format_preview_threads_caption(p)
    assert len(threads.encode("utf-16-le")) // 2 <= 500
    assert "Формат:" in threads and "📍" in threads
    assert all(stage.format_text in threads for stage in p.stages)
    assert p.passport_source_url in threads


def test_threads_overflow_is_rejected_without_truncation():
    p = preview().model_copy(update={"story": "Очень длинная история " * 40})
    with pytest.raises(PreviewUnavailable, match="threads_caption_too_long"):
        format_preview_threads_caption(p)


def social_runtime(tmp_path, monkeypatch, *, failure=None):
    r, calls = runtime(tmp_path, monkeypatch)
    r.instagram_publishing_enabled = r.threads_publishing_enabled = lambda: True
    class Uncertain(Exception):
        pass
    r.InstagramDeliveryUncertainError = r.ThreadsDeliveryUncertainError = Uncertain
    sent = []
    def instagram(uid, cards, caption, context):
        sent.append(("instagram", uid, cards, caption, context))
        if failure == "instagram":
            raise RuntimeError("rejected")
        return "instagram-post-id"
    def threads(key, uid, cards, caption, context):
        sent.append(("threads", uid, cards, caption, context, key))
        if failure == "threads":
            raise Uncertain()
        return "threads-post-id"
    r.publish_rendered_cards, r._publish_threads_chain = instagram, threads
    return r, calls, sent


def test_all_platforms_publish_independently_with_context_and_stable_identity(tmp_path, monkeypatch):
    r, calls, sent = social_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(job, "render_preview_cards", lambda *args: [b"png", b"teams"])
    r.send_media_group_to_telegram = lambda *args, **kwargs: calls.append("send")
    context = {"token": "test-token"}
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW, context=context)["body"])
    assert body["messages_sent"] == 3 and body["delivery_failures"] == 0
    outcomes = body["previews"][0]["deliveries"]
    assert outcomes["instagram"]["post_id"] == "instagram-post-id"
    assert outcomes["threads"]["post_id"] == "threads-post-id"
    assert all(len(item[2]) == 2 and item[4] is context for item in sent)
    assert sent[0][1] == "instagram_tournament_preview_test-2026"
    assert sent[1][5] == "threads:preview:liquipedia:Test/2026"


def test_telegram_text_flag_does_not_disable_social_cards(tmp_path, monkeypatch):
    r, calls, sent = social_runtime(tmp_path, monkeypatch)
    r.TELEGRAM_MEDIA_CARDS = False
    r.send_to_telegram = lambda *args: calls.append("send")
    rendered = []
    monkeypatch.setattr(job, "render_preview_cards", lambda *args: rendered.append(True) or [b"png"])
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    assert rendered == [True] and body["messages_sent"] == 3
    assert all(item[2] == [b"png"] for item in sent)


@pytest.mark.parametrize("failure,expected_status", [("instagram", "failed"), ("threads", "uncertain")])
def test_social_failure_keeps_other_platforms_working(tmp_path, monkeypatch, failure, expected_status):
    r, calls, sent = social_runtime(tmp_path, monkeypatch, failure=failure)
    body = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    assert body["messages_sent"] == 2 and body["delivery_failures"] == 1
    assert body["previews"][0]["deliveries"][failure]["status"] == expected_status
    assert len(sent) == 2 and calls.count("send") == 1
    assert ("retain" in calls) == (failure == "threads")
    assert ("release" in calls) == (failure == "instagram")


def test_social_deduplication_is_separate_from_telegram(tmp_path, monkeypatch):
    r, calls, sent = social_runtime(tmp_path, monkeypatch)
    processed = {"preview_test-2026_c"}
    async def reconcile(uid, *args):
        return uid in processed
    async def mark(uid, *args):
        processed.add(uid)
    r.reconcile_content_delivery, r.mark_content_processed = reconcile, mark
    first = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    second = json.loads(job.run_preview_job("test-2026", False, r, now=NOW)["body"])
    assert first["messages_sent"] == 2 and first["duplicates_skipped"] == 1
    assert second["messages_sent"] == 0 and second["duplicates_skipped"] == 3
    assert len(sent) == 2
