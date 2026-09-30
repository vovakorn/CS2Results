from datetime import date
from types import SimpleNamespace

import pytest

from cs2bot.x_content import (
    XContentError,
    build_x_digest_text,
    build_x_result_text,
    build_x_schedule_text,
)


def _upcoming(team1="NAVI", team2="FaZe", tournament="IEM Cologne"):
    return SimpleNamespace(
        team1_name=team1,
        team2_name=team2,
        tournament_name=tournament,
        scheduled_at="2026-09-29T12:30:00+00:00",
    )


def _result(team1="NAVI", team2="FaZe", tournament="IEM Cologne"):
    return SimpleNamespace(
        team1_name=team1,
        team2_name=team2,
        tournament_name=tournament,
        score1=2,
        score2=1,
        date="2026-09-29T15:45:00Z",
        end_date=None,
    )


@pytest.mark.parametrize("image_count", [1, 4])
def test_schedule_preserves_date_and_long_team_names(image_count):
    team1 = "Very Long Team Name " * 3
    team2 = "Another Long Team Name " * 3
    match = _upcoming(team1=team1, team2=team2, tournament="P" * 300)

    text = build_x_schedule_text([match], date(2026, 9, 29), [b"image"] * image_count)

    assert len(text) <= 240
    assert "2026-09-29" in text
    assert "15:30" in text
    assert team1.strip() in text
    assert team2.strip() in text
    assert "P" * 300 not in text


@pytest.mark.parametrize("image_count", [1, 4])
def test_result_preserves_names_score_and_match_date(image_count):
    team1 = "Very Long Team Name " * 3
    team2 = "Another Long Team Name " * 3
    match = _result(team1=team1, team2=team2, tournament="P" * 300)

    text = build_x_result_text(match, [b"image"] * image_count)

    assert len(text) <= 240
    assert "2026-09-29" in text
    assert team1.strip() in text
    assert team2.strip() in text
    assert "2:1" in text
    assert "P" * 300 not in text


@pytest.mark.parametrize("image_count", [1, 4])
def test_digest_preserves_each_match_date_names_and_score(image_count):
    first = _result(team1="NAVI", team2="FaZe")
    second = _result(team1="Spirit", team2="Vitality")
    second.date = "2026-09-28"

    text = build_x_digest_text(
        [first, second], date(2026, 9, 29), [b"image"] * image_count
    )

    assert len(text) <= 240
    assert "Итоги CS2 · 2026-09-29" in text
    assert "2026-09-29 NAVI 2:1 FaZe" in text
    assert "2026-09-28 Spirit 2:1 Vitality" in text


@pytest.mark.parametrize("builder, args", [
    (build_x_schedule_text, ([], date(2026, 9, 29), [b"image"])),
    (build_x_digest_text, ([], date(2026, 9, 29), [b"image"])),
])
def test_empty_match_lists_raise_explicit_error(builder, args):
    with pytest.raises(XContentError, match="at least one"):
        builder(*args)


@pytest.mark.parametrize("builder, args", [
    (build_x_schedule_text, ([_upcoming()], date(2026, 9, 29))),
    (build_x_result_text, (_result(),)),
    (build_x_digest_text, ([_result()], date(2026, 9, 29))),
])
@pytest.mark.parametrize("image_count", [0, 5])
def test_invalid_image_counts_raise_and_never_trim_the_album(builder, args, image_count):
    with pytest.raises(XContentError, match="image"):
        builder(*args, [b"image"] * image_count)


def test_required_content_over_limit_raises_instead_of_truncating_names():
    match = _result(team1="A" * 130, team2="B" * 130)

    with pytest.raises(XContentError, match="limit is 240"):
        build_x_result_text(match, [b"image"])


def test_result_with_missing_score_raises_explicit_error():
    match = _result()
    match.score2 = None

    with pytest.raises(XContentError, match="both scores"):
        build_x_result_text(match, [b"image"])
