"""Prepare three regional VRS histories from the saved public response, offline."""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import preview_vrs_major_history as history
from regional import cohorts
from music import render as render_music, signature as music_signature

HERE = Path(__file__).resolve().parent
SOURCE = history.DEFAULT_OUTPUT
PUBLIC = HERE / 'public'
PUBLIC.mkdir(exist_ok=True)
data = json.loads((SOURCE / 'data.json').read_text())
history.validate(data)
raw_path = SOURCE / 'sources/vrs_ranking_live.json'
raw = json.loads(raw_path.read_text())
regions = cohorts(raw, data['palette'], history.same_core)
logos = dict(data['logos'])
plates = set(data['logo_plates'])
for asset in json.loads((HERE / 'assets/logos/manifest.json').read_text()):
    with Image.open(HERE / 'assets/logos' / asset['file']) as source:
        rgba = source.convert('RGBA')
        png = io.BytesIO()
        rgba.save(png, format='PNG')
        logos[asset['name']] = 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()
        visible = [(r,g,b) for r,g,b,a in rgba.getdata() if a > 180]
        if visible and sum(.2126*r+.7152*g+.0722*b for r,g,b in visible)/len(visible) < 60:
            plates.add(asset['name'])
for region in regions:
    region.update(logos=logos, logoPlates=sorted(plates), sourceLabel='LIQUIPEDIA VRS',
                  inputSha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                  collectedOn=data['collected_on'], sourceType='vrs_ranking_live')
    folder = SOURCE / 'regional-reels' / region['regionSlug']
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'data.json').write_text(json.dumps(region, ensure_ascii=False, indent=2))
    with (folder / 'history.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['date','tracked_core','display_name','region','regional_rank','global_rank','points','roster','template'])
        for snapshot in region['snapshots']:
            for row in snapshot['rows']:
                writer.writerow([snapshot['date'],row['name'],row['displayName'],row['region'],row['rank'],
                                 row['globalRank'],row['points'],' | '.join(row['roster']),row['template']])
(PUBLIC / 'regional-data.json').write_text(json.dumps(regions, ensure_ascii=False))
shutil.copyfile(ROOT / 'cs2bot/assets/fonts/RussoOne-Regular.ttf', PUBLIC / 'RussoOne-Regular.ttf')
shutil.copyfile(ROOT / 'cs2bot/assets/channel-logo.png', PUBLIC / 'channel-logo.png')
seconds = 1.8 + 1.8 * (len(regions[0]['snapshots'])-1) + 1.2 + 4.2
signature = music_signature(seconds)
marker = PUBLIC/'music-signature.txt'
if not (PUBLIC/'original-soundtrack.wav').exists() or not marker.exists() or marker.read_text() != signature:
    render_music(seconds)
    marker.write_text(signature)
print(json.dumps({'regions':{r['regionSlug']:len(r['names']) for r in regions},
                  'snapshots':len(regions[0]['snapshots']), 'duration':seconds,
                  'sourceEnd':regions[0]['snapshots'][-1]['date']}))
