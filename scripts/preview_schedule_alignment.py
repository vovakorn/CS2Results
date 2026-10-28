"""Render the approved alignment using real templates and offline logo fixtures."""
import argparse
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import sys
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cs2bot import media_cards as m, schedule_reels as r
from scripts.reel_demo_fixtures import demo_fixtures, demo_logo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    now = datetime.fromisoformat('2026-10-28T09:00:00+03:00')
    files = []
    def logo(url, **kwargs):
        if url in ('demo://light', 'demo://dark'):
            return Image.new('RGBA', (100, 100), 'white' if url.endswith('light') else 'black')
        return demo_logo(url, **kwargs)
    def save(data, name):
        image = Image.open(BytesIO(data)).convert('RGB') if isinstance(data, bytes) else data
        if image.height != 1920:
            m._draw_text_block(ImageDraw.Draw(image), 540, image.height - 27,
                              'ДЕМО · НЕ ДЛЯ ПУБЛИКАЦИИ', 900, 16, display=True, fill=m.AMBER)
        image.save(args.output / name)
        files.append(name)
        return image
    with patch.object(m, 'fetch_team_logo', logo):
        for variant in ('normal', 'long', 'mixed'):
            for count in (1, 4, 10):
                fixtures = demo_fixtures(count)
                if variant == 'long':
                    fixtures = [match.model_copy(update={'team1_name':'Gaimin Gladiators Academy',
                        'team2_name':'Natus Vincere Junior', 'team1_logo_url':None if i % 2 else 'demo://light',
                        'team2_logo_url':'demo://dark'}) for i, match in enumerate(fixtures)]
                elif variant == 'mixed':
                    fixtures = [match.model_copy(update={'competition_key':'Unknown Community Cup',
                        'tournament_name':'Unknown Community Cup'}) if i % 2 else match for i, match in enumerate(fixtures)]
                for form in ('square', 'portrait'):
                    pages = m.render_schedule_cards(fixtures, now, 'Europe/Moscow', card_format=form)
                    for page, data in enumerate(pages, 1):
                        image = save(data, f'schedule-{variant}-{count}-{form}-{page}.png')
                        if variant == 'normal' and count == 4 and form == 'portrait':
                            image.save(args.output / 'selected-schedule.png')
                    save(m.render_schedule_context_covers(fixtures, now, card_format=form)[0],
                         f'context-{variant}-{count}-{form}.png')
                for scene in r.storyboard(fixtures):
                    if scene.kind == 'matches':
                        image = save(r.render_scene(scene, now, count, preview_watermark=True),
                                     f'reel-{variant}-{count}-{scene.page}.png')
                        if variant == 'normal' and count == 4:
                            image.save(args.output / 'selected-reel.png')
    (args.output / 'manifest.json').write_text(json.dumps({'renders':len(files), 'files':files,
        'runtime_templates':True, 'published':False}, indent=2)+'\n')
    print(f'{len(files)} offline renders saved to {args.output}')


if __name__ == '__main__':
    main()
