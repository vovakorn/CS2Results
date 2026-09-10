import json
from pathlib import Path

from cs2bot.match_sources.filters import is_valid_match
from cs2bot.match_sources.models import MatchNormalized


FIXTURE = Path(__file__).parent / "fixtures" / "normalized_finished_matches.json"


def test_normalized_finished_match_fixtures_follow_the_public_contract():
    matches = [MatchNormalized.model_validate(item) for item in json.loads(FIXTURE.read_text())]

    assert {match.source for match in matches} == {"pandascore", "liquipedia"}
    assert all(is_valid_match(match) == (True, None) for match in matches)
    assert [(item.name, item.score1, item.score2) for item in matches[0].maps] == [
        ("Mirage", 13, 11),
        ("Ancient", 7, 13),
        ("Inferno", 13, 10),
    ]
