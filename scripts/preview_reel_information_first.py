"""Offline proofs of the first-frame schedule, covers and complete team pages."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as m, schedule_reels as r
from scripts.reel_demo_fixtures import demo_fixtures, demo_logo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--encode-counts', type=int, nargs='*', default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat('2026-10-28T09:00:00+03:00')
    cases = []
    with patch.object(m, 'fetch_team_logo', demo_logo):
        for count in (1, 4, 10, 20):
            matches = demo_fixtures(count)
            if count == 20:
                matches = [match.model_copy(update={'team1_name':f'Demo Team {i * 2 + 1}',
                    'team2_name':f'Demo Team {i * 2 + 2}', 'team1_logo_url':None, 'team2_logo_url':None})
                    for i, match in enumerate(matches)]
            scenes = r.storyboard(matches)
            duration = sum(scene.duration for scene in scenes)
            visuals = r._resolve_reel_visuals(matches)
            first = scenes[0]
            assert first.kind == 'matches'
            for index, (frame, _) in enumerate(r.animated_scene_frames(first, now, count,
                    'Europe/Moscow', elapsed_before=0, total_seconds=duration,
                    preview_watermark=True, visuals=visuals)):
                if index in (0, 6):
                    frame.save(args.output / f'opening-{count}-{index}.png')
                if index == 6:
                    break
            teams = [scene for scene in scenes if scene.kind == 'intro']
            for scene in teams:
                r.render_scene(scene, now, count, preview_watermark=True, visuals=visuals).save(
                    args.output / f'teams-{count}-{scene.page}.png')
            row = {'matches':count, 'duration_seconds':duration, 'scene_order':[scene.kind for scene in scenes],
                'unique_teams':len(r._intro_teams(matches)), 'team_pages':len(teams), 'first_frame_has_schedule':True,
                'first_schedule_at_seconds':0, 'published':False}
            if count in args.encode_counts:
                started = time.monotonic()
                data = r.render_schedule_reel(matches, now, preview_watermark=True, audio_style='minimal')
                path = args.output / f'reel-{count}.mp4'
                path.write_bytes(data)
                ffmpeg = r._ffmpeg_executable()
                decode = subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(path),
                    '-f','null','-'], capture_output=True, check=True, timeout=120)
                for at, label in (('0','first'),('0.5','cover')):
                    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-ss',at,'-i',str(path),
                        '-frames:v','1',str(args.output / f'decoded-{label}-{count}.png')],
                        capture_output=True, check=True, timeout=30)
                row.update(mp4_bytes=len(data), render_and_full_decode_ms=round((time.monotonic()-started)*1000),
                           full_decode_passed=decode.returncode==0)
            cases.append(row)
    (args.output / 'opening-evidence.json').write_text(json.dumps(cases,indent=2)+'\n')
    print(json.dumps(cases))


if __name__ == '__main__':
    main()
