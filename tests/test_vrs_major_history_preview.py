"""Offline checks for the preview's source and identity boundaries."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/preview_vrs_major_history.py'
spec = importlib.util.spec_from_file_location('vrs_major_preview', SCRIPT)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


def row(name='Falcons', rank=1, points=100, roster=None):
    return {'name': name, 'rank': rank, 'points': points,
            'roster': roster or ['a', 'b', 'c', 'd', 'e']}


def dataset():
    rows = [row()]
    return {'snapshots': [{'date': '2026-07-06', 'rows': rows}],
            'liquipedia_snapshots': [],
            'official': [{'date': '2026-07-06', 'rows': deepcopy(rows), 'sha': 'saved-blob'}]}


def test_continuity_needs_three_distinct_nonempty_players():
    before = row(roster=[' A ', 'B', 'c', 'd', 'e'])
    assert preview.same_core(before, row(roster=['a', 'b', 'C', 'f', 'g']))
    assert not preview.same_core(before, row(roster=['a', 'b', 'f', 'g', 'h']))
    assert not preview.same_core(row(roster=['a', 'a', '', 'b']),
                                 row(roster=['a', 'a', '', 'b']))


def test_missing_team_is_not_a_zero_observation():
    assert preview.observations({'date': '2026-07-06', 'rows': [row()]}).get('spirit') is None


def test_case_only_names_are_ambiguous():
    with pytest.raises(ValueError, match='Ambiguous team identity'):
        preview.observations({'date': '2026-07-06', 'rows': [row(), row('FALCONS', 2)]})


def test_official_comparison_checks_points_and_rank():
    data = dataset()
    assert preview.validate(data)['official_checks'][0]['matched']
    data['snapshots'][0]['rows'][0]['points'] += 1
    with pytest.raises(ValueError, match='Official snapshot mismatch'):
        preview.validate(data)


@pytest.mark.parametrize('change', ['duplicate_date', 'duplicate_rank', 'zero_points'])
def test_bad_source_observations_fail_closed(change):
    data = dataset()
    if change == 'duplicate_date':
        data['snapshots'].append(deepcopy(data['snapshots'][0]))
    elif change == 'duplicate_rank':
        data['snapshots'][0]['rows'].append(row('Spirit', 1))
    else:
        data['snapshots'][0]['rows'][0]['points'] = 0
    with pytest.raises(ValueError):
        preview.validate(data)


def test_liquipedia_keeps_fractional_points_and_unobserved_dates():
    opponent = {'template': 'team falcons 2022', 'players': {
        '1': {'displayName': 'a'}, '2': {'displayName': 'b'}, '3': {'displayName': 'c'}},
        'extradata': {'region': 'Europe'}}
    raw = {'response': {'result': [
        {'date': '2026-07-13 00:00:00', 'extradata': {'1': {
            'place': 2, 'points': 1989.4, 'opponent': opponent}}},
        {'date': '2026-06-22 00:00:00', 'extradata': {'1': {
            'place': 1, 'points': 1990.1, 'opponent': opponent}}}]}}
    snapshots = preview.normalize_lp(raw)
    assert [s['date'] for s in snapshots] == ['2026-06-22', '2026-07-13']
    assert snapshots[-1]['rows'][0]['points'] == 1989.4
    assert snapshots[-1]['kind'] == 'unofficial'
    assert snapshots[-1]['rows'][0]['roster'] == ['a', 'b', 'c']
