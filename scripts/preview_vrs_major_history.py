"""Offline VRS history preview. No publishing, credentials or network requests."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import subprocess
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'output/vrs-major-race-2026-10-12'
FONT = '/System/Library/Fonts/Supplemental/Arial.ttf'
BOLD = '/System/Library/Fonts/Supplemental/Arial Bold.ttf'
PALETTE = {
    'Spirit': '#b5eee5', 'Falcons': '#67da9a', 'Vitality': '#f6cf57',
    'Natus Vincere': '#e9df92', 'FURIA': '#eceff4', 'MOUZ': '#ed6593',
    'Legacy': '#94b86b', 'Aurora': '#6ecbf0', 'G2': '#e47762',
    'FUT': '#bc9df1', 'BETBOOM': '#eab66f', '9z': '#b580da',
    'PARIVISION': '#729fd8', 'FaZe': '#dd8f98', 'B8': '#b6b2a6',
    'MIBR': '#85a9a3', 'Astralis': '#bd7474',
}
LOGO_PLATES = {'Vitality', 'Spirit', 'B8', 'MIBR'}
# Reviewed Liquipedia template IDs. Dates in template names identify branding,
# not an immutable five-player VRS roster.
LP_NAMES = {
    'team spirit 2022': 'Spirit', 'team falcons 2022': 'Falcons',
    'team vitality 2023': 'Vitality', 'natus vincere 2021': 'Natus Vincere',
    'furia 2025': 'FURIA', 'mouz 2021': 'MOUZ', 'legacy 2023': 'Legacy',
    'aurora gaming 2025': 'Aurora', 'g2 esports 2019': 'G2',
    'fut esports': 'FUT', 'betboom team 2024': 'BETBOOM',
    '9z team 2024': '9z', 'parivision': 'PARIVISION',
    'faze clan 2026': 'FaZe', 'b8': 'B8', 'mibr': 'MIBR',
    'astralis 2020': 'Astralis',
}


def team_key(name):
    return name.casefold()


def observations(snapshot):
    rows = {}
    for row in snapshot['rows']:
        key = team_key(row['name'])
        if key in rows:
            raise ValueError(f"Ambiguous team identity: {snapshot['date']} / {row['name']}")
        rows[key] = row
    return rows


def same_core(before, after):
    def players(row):
        return {p.strip().casefold() for p in row['roster'] if p.strip()}
    return len(players(before) & players(after)) >= 3


def cohort(data):
    keys = {team_key(r['name']) for s in data['snapshots'] for r in s['rows'] if r['rank'] <= 12}
    return [name for name in PALETTE if team_key(name) in keys]


def validate(data):
    audit = {'official_checks': [], 'source_rows': {}, 'cohort_size': len(cohort(data))}
    for source, snaps in (('HLTV', data['snapshots']), ('LiquipediaDB', data.get('liquipedia_snapshots', []))):
        dates = [s['date'] for s in snaps]
        if dates != sorted(set(dates)):
            raise ValueError('Snapshot dates must be sorted and unique')
        for snap in snaps:
            rows = observations(snap)
            if len({r['rank'] for r in rows.values()}) != len(rows):
                raise ValueError('Duplicate ranks')
            if any(r['points'] <= 0 or r['rank'] < 1 for r in rows.values()):
                raise ValueError('Invalid points/rank')
        audit['source_rows'][source] = sum(len(s['rows']) for s in snaps)
    for official in data['official']:
        snap = next(s for s in data['snapshots'] if s['date'] == official['date'])
        left = [(r['rank'], team_key(r['name']), r['points']) for r in official['rows'][:40]]
        right = [(r['rank'], team_key(r['name']), r['points']) for r in snap['rows']]
        if left != right:
            raise ValueError(f"Official snapshot mismatch: {official['date']}")
        audit['official_checks'].append({'date': official['date'], 'rows': 40, 'matched': True,
                                         'blob_sha': official['sha']})
    return audit


def normalize_lp(raw):
    snaps = []
    for record in raw['response']['result']:
        rows = []
        for row in record['extradata'].values():
            opp = row['opponent']
            name = LP_NAMES.get(opp.get('template'))
            if name is None:
                continue
            players = opp.get('players', {})
            players = players.values() if isinstance(players, dict) else players
            rows.append({'rank': row['place'], 'name': name, 'points': row['points'],
                         'region': opp.get('extradata', {}).get('region'),
                         'roster': [p.get('displayName', '') for p in players],
                         'template': opp.get('template')})
        stamp = record['date'][:10]
        snaps.append({'date': stamp, 'source': 'LiquipediaDB VRS', 'kind': 'unofficial',
                      'url': 'sources/vrs_ranking_live.json',
                      'page_url': 'https://liquipedia.net/counterstrike/Valve_Regional_Standings',
                      'rows': sorted(rows, key=lambda r: r['rank'])})
    return sorted(snaps, key=lambda s: s['date'])


def font(size, bold=False):
    candidates = [BOLD if bold else FONT,
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
                  else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                  ROOT / 'cs2bot/assets/fonts/RussoOne-Regular.ttf']
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(str(candidate), size)
    raise RuntimeError('A font with Cyrillic glyphs is required')


FONTS = {(size, bold): font(size, bold) for size in (20, 22, 24, 26, 28, 30, 32, 38, 48, 68, 82) for bold in (False, True)}


def fit_labels(ys, lo, hi, gap):
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    placed = [0.0] * len(ys)
    for j, index in enumerate(order):
        placed[index] = max(ys[index], lo if j == 0 else placed[order[j-1]] + gap)
    if order:
        placed[order[-1]] = min(placed[order[-1]], hi)
        for j in range(len(order) - 2, -1, -1):
            index = order[j]
            placed[index] = min(placed[index], placed[order[j+1]] - gap)
    return placed


class Renderer:
    def __init__(self, data, output, source='hltv', size=(1080, 1920)):
        self.data = data
        self.output = output
        self.source = source
        self.snaps = data['snapshots'] if source == 'hltv' else data['liquipedia_snapshots']
        self.names = cohort(data)
        self.rows = [observations(s) for s in self.snaps]
        self.days = [(date.fromisoformat(s['date']) - date.fromisoformat(self.snaps[0]['date'])).days for s in self.snaps]
        self.max_rank = max(self.row(i, name)['rank'] for i in range(len(self.snaps)) for name in self.names if self.row(i, name))
        self.max_rank = int(math.ceil(self.max_rank / 5) * 5)
        self.size = size
        self.logos = {}
        for name in self.names:
            logo_path = output / 'logos' / (name.lower().replace(' ', '-') + '.png')
            if logo_path.exists():
                logo = Image.open(logo_path).convert('RGBA')
                logo.thumbnail((30, 30), Image.Resampling.LANCZOS)
                self.logos[name] = logo

    def row(self, i, name):
        return self.rows[i].get(team_key(name))

    def x(self, day):
        return 110 + 570 * day / self.days[-1]

    def y(self, rank):
        return 490 + (rank - 1) / (self.max_rank - 1) * 920

    def frame(self, index, frac=0):
        image = Image.new('RGB', (1080, 1920), '#0c151d')
        d = ImageDraw.Draw(image)
        def text(x, y, value, size=26, color='#eaf0f5', bold=False, anchor=None):
            d.text((x, y), value, font=FONTS[(size, bold)], fill=color, anchor=anchor)
        d.rounded_rectangle((60, 70, 1020, 116), radius=4, fill='#15252e')
        text(78, 80, 'CS2 RESULTS  /  VRS', 22, '#c9d6db', True)
        text(1000, 80, 'COLOGNE → SINGAPORE', 20, '#8497a2', anchor='ra')
        text(60, 155, 'ДОРОГА К MAJOR', 82, bold=True)
        text(62, 259, 'Как команды меняли места и очки VRS', 28, '#b6c5cc')
        next_index = min(index + 1, len(self.snaps) - 1)
        current = self.snaps[index]
        current_date = date.fromisoformat(current['date']).strftime('%d.%m.%Y')
        text(62, 328, current_date, 48, bold=True)
        kind = 'ОФИЦИАЛЬНЫЙ СНИМОК' if current['kind'] == 'official' else ('HLTV LIVE VRS' if self.source == 'hltv' else 'LIQUIPEDIA VRS · НЕОФИЦИАЛЬНЫЙ')
        text(62, 397, kind, 20, '#8ca2b0')
        if frac > 0:
            text(1008, 346, 'Переход к ' + date.fromisoformat(self.snaps[next_index]['date']).strftime('%d.%m'), 22, '#8ca2b0', anchor='ra')
        text(62, 449, 'МЕСТО В МИРЕ', 20, '#8ca2b0', True)
        text(1010, 449, 'ОЧКИ VRS', 20, '#8ca2b0', True, anchor='ra')
        for rank in [1] + list(range(5, self.max_rank + 1, 5)):
            y = self.y(rank)
            d.line((106, y, 703, y), fill='#23343f', width=1)
            text(90, y, str(rank), 22, '#8094a1', anchor='rm')
        for tick in [0, 7, 15, len(self.snaps)-1]:
            if tick >= len(self.snaps):
                continue
            x = self.x(self.days[tick])
            d.line((x, 476, x, 1428), fill='#182832', width=1)
            text(x, 1453, date.fromisoformat(self.snaps[tick]['date']).strftime('%d.%m'), 22, '#8094a1', anchor='ma')
        for k, snap in enumerate(self.snaps):
            if snap['kind'] == 'official':
                x = self.x(self.days[k]); d.ellipse((x-4, 1495, x+4, 1503), fill='#e0be72')
        endpoints = []
        for name in self.names:
            color = PALETTE[name]
            key = team_key(name)
            previous = None
            for k in range(index + 1):
                row = self.row(k, name)
                if row is None:
                    previous = None; continue
                xy = (self.x(self.days[k]), self.y(row['rank']))
                if previous and same_core(self.row(k-1, name), row):
                    d.line((*previous, *xy), fill=color, width=3)
                d.ellipse((xy[0]-2, xy[1]-2, xy[0]+2, xy[1]+2), fill=color)
                previous = xy
            row, after = self.row(index, name), self.row(next_index, name)
            if row is None:
                continue
            rank = row['rank']
            day = self.days[index]
            if frac and after and same_core(row, after):
                day += (self.days[next_index] - day) * frac
                rank += (after['rank'] - rank) * frac
                d.line((self.x(self.days[index]), self.y(row['rank']), self.x(day), self.y(rank)), fill=color, width=3)
            endpoints.append((name, row, self.x(day), self.y(rank)))
        label_ys = fit_labels([p[3] for p in endpoints], 491, 1406, 49)
        for (name, row, x, y), ly in zip(endpoints, label_ys):
            color = PALETTE[name]
            d.line((x+9, y, 719, ly, 730, ly), fill=color, width=1)
            d.ellipse((x-7, y-7, x+7, y+7), fill='#0c151d', outline=color, width=3)
            if name in self.logos:
                logo = self.logos[name]
                if name in LOGO_PLATES:
                    d.ellipse((738, ly-18, 774, ly+18), fill='#eaf0f5')
                image.paste(logo, (741 + (30-logo.width)//2, round(ly)-15), logo)
            short = 'NAVI' if name == 'Natus Vincere' else ('BetBoom' if name == 'BETBOOM' else name)
            text(781, ly-3, short, 22, color, True, anchor='lm')
            value = str(row['points']) if self.source == 'hltv' else f"{row['points']:.1f}"
            text(1010, ly-3, value, 24, color, True, anchor='rm')
        text(62, 1533, f'{len(self.names)} команд, входивших в топ-12 за период', 24, '#b6c5cc')
        falcons = self.row(index, 'Falcons')
        baseline = self.row(0, 'Falcons')
        d.rounded_rectangle((60, 1595, 1020, 1742), radius=8, fill='#162c28')
        text(82, 1614, 'FALCONS · ЧЕМПИОН COLOGNE', 22, '#82d5ac', True)
        if falcons:
            pts = falcons['points']; delta = pts-baseline['points']
            detail = f"#{falcons['rank']}  /  {pts:g} очков  /  {delta:+g} с начала"
            text(82, 1668, detail, 38, '#d8f4e8', True)
        label = 'HLTV live VRS · недельные точки' if self.source == 'hltv' else 'LiquipediaDB · недельные точки'
        text(62, 1783, label, 22, '#8ca2b0')
        text(62, 1821, 'Линии между снимками — анимация перехода.', 20, '#8ca2b0')
        text(62, 1855, 'Отбор на Singapore: снимок 02.11.2026', 20, '#8ca2b0')
        if self.size != (1080, 1920):
            image = image.resize(self.size, Image.Resampling.LANCZOS)
        return image


def write_csv(data, output):
    for key, name in (('snapshots', 'hltv-history.csv'), ('liquipedia_snapshots', 'liquipedia-history.csv')):
        with (output / name).open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'team', 'global_rank', 'vrs_points', 'region', 'roster', 'source', 'kind', 'url'])
            for snap in data[key]:
                for row in snap['rows']:
                    writer.writerow([snap['date'], row['name'], row['rank'], row['points'], row['region'],
                                     ', '.join(row['roster']), snap['source'], snap['kind'], snap['url']])


def build(output):
    data = json.loads((output / 'data.json').read_text())
    data['liquipedia_snapshots'] = normalize_lp(json.loads((output / 'sources/vrs_ranking_live.json').read_text()))
    data['cohort'] = cohort(data)
    data['palette'] = PALETTE
    data['logo_plates'] = sorted(LOGO_PLATES)
    for row in data['snapshots']:
        row['rows'] = [dict(r, name='BETBOOM') if team_key(r['name']) == 'betboom' else r for r in row['rows']]
    audit = validate(data)
    data['logos'] = {}
    for name in data['cohort']:
        path = output / 'logos' / (name.lower().replace(' ', '-') + '.png')
        if path.exists():
            with Image.open(path) as im:
                import io
                buf = io.BytesIO(); im.convert('RGBA').save(buf, format='PNG')
                data['logos'][name] = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    (output / 'data.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
    write_csv(data, output)
    template = (ROOT / 'scripts/vrs_major_history_preview.html').read_text()
    (output / 'preview.html').write_text(template.replace('__VRS_DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/')))
    (output / 'validation.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    for source in ('hltv', 'liquipedia'):
        renderer = Renderer(data, output, source)
        for index, label in ((0, 'start'), (len(renderer.snaps)//2, 'middle'), (len(renderer.snaps)-1, 'latest')):
            renderer.frame(index).save(output / f'{source}-{label}.png')
    return data


def render_video(data, output, source, fps=24):
    import imageio_ffmpeg
    renderer = Renderer(data, output, source)
    filename = output / f'{source}-vrs-history.mp4'
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg, '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
               '-s', '1080x1920', '-r', str(fps), '-i', '-', '-an', '-c:v', 'libx264',
               '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(filename)]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for index in range(len(renderer.snaps)):
            hold = 2 if index in (0, len(renderer.snaps)-1) else 0.7
            frame = renderer.frame(index).tobytes()
            for _ in range(round(hold*fps)):
                proc.stdin.write(frame)
            if index < len(renderer.snaps)-1:
                for j in range(1, round(1.05*fps)+1):
                    frac = j / round(1.05*fps)
                    proc.stdin.write(renderer.frame(index, frac).tobytes())
            if index % 4 == 0:
                print(f'{source}: snapshot {index+1}/{len(renderer.snaps)}', flush=True)
        proc.stdin.close()
        error = proc.stderr.read().decode()
        if proc.wait() != 0:
            raise RuntimeError(error)
    except Exception:
        proc.kill(); proc.wait(); raise
    return filename


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--video', choices=('hltv', 'liquipedia', 'both'))
    args = parser.parse_args()
    data = build(args.output)
    if args.video:
        for source in (('hltv', 'liquipedia') if args.video == 'both' else (args.video,)):
            print(render_video(data, args.output, source), flush=True)


if __name__ == '__main__':
    main()
