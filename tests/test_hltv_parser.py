from pathlib import Path

import pytest

from cs2bot.match_sources.models import SourceUnavailableError
from cs2bot.match_sources.legacy.hltv_results_source import parse_results_page


FIXTURE = Path(__file__).parent / "fixtures" / "hltv_results_sample.html"


def test_parser_returns_matches():
    matches = parse_results_page(FIXTURE.read_text(), limit=10)
    assert len(matches) == 2


def test_each_match_has_required_publication_fields():
    matches = parse_results_page(FIXTURE.read_text(), limit=10)
    for match in matches:
        assert match.team1_name
        assert match.team2_name
        assert match.score1 is not None
        assert match.score2 is not None
        assert match.tournament_name
        assert match.match_id or match.match_url
        assert isinstance(match.maps, list)


def test_bad_match_does_not_break_parser():
    matches = parse_results_page(FIXTURE.read_text(), limit=10)
    assert [match.match_id for match in matches] == ["2378481", "2378482"]


def test_limit_restricts_results():
    matches = parse_results_page(FIXTURE.read_text(), limit=1)
    assert len(matches) == 1


def test_parser_raises_when_hltv_markup_is_not_recognized():
    with pytest.raises(SourceUnavailableError):
        parse_results_page("<html><body>blocked or changed</body></html>")
