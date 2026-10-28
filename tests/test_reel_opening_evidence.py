"""The preview report must detect late or visually hidden opening information."""
import json
import sys

import pytest

from cs2bot import schedule_reels as r
from scripts import preview_reel_information_first as preview


@pytest.mark.parametrize('regression', ['late_schedule', 'hidden_rows'])
def test_preview_report_records_failure_instead_of_hardcoded_success(tmp_path, monkeypatch, regression):
    if regression == 'late_schedule':
        original = r.storyboard
        def storyboard(matches):
            scenes = original(matches)
            return tuple(scene for kind in ('intro', 'matches', 'outro') for scene in scenes if scene.kind == kind)
        monkeypatch.setattr(r, 'storyboard', storyboard)
    else:
        original = r.animated_scene_frames
        def frames(*args, **kwargs):
            kwargs['elapsed_before'] = .01
            return original(*args, **kwargs)
        monkeypatch.setattr(r, 'animated_scene_frames', frames)
    monkeypatch.setattr(sys, 'argv', ['preview', '--output', str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        preview.main()
    assert error.value.code == 1
    cases = json.loads((tmp_path / 'opening-evidence.json').read_text())
    assert len(cases) == 4
    assert all(row['first_frame_has_schedule'] is False for row in cases)
    if regression == 'late_schedule':
        assert all(row['first_schedule_at_seconds'] > 0 for row in cases)
    else:
        assert all(row['first_schedule_at_seconds'] == 0 for row in cases)
        assert any(row['cover_has_schedule'] is False for row in cases)
