"""Offline visual refresh examples; fictional scores, fixtures and VRS values."""
import argparse
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import (UpcomingMatchNormalized, MatchNormalized, MapResult,
    SourceReferences, TournamentVRSImpact)
from cs2bot.tournament_preview import load_profiles
from preview_schedule_reels import _synthetic_logo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--encode-reel', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    m.fetch_team_logo = _synthetic_logo
    now = datetime(2026, 10, 3, 9, tzinfo=ZoneInfo('Europe/Moscow'))
    profile = load_profiles(ROOT / 'data/tournament_preview_profiles.json')[0]
    pairs = [('Team Spirit','FUT Esports'), ('NAVI','MOUZ'), ('G2 Esports','FaZe Clan'),
             ('Gaimin Gladiators Academy','Natus Vincere Junior'), ('Vitality','The MongolZ')]
    fixtures = [UpcomingMatchNormalized(match_id=f'demo-{i}',
        tournament_name='ESL Pro League Season 24', competition_key='ESL Pro League Season 24',
        source_refs=SourceReferences(serie_id=str(profile.pandascore_serie_id)),
        team1_name=pairs[i%5][0], team2_name=pairs[i%5][1],
        team1_logo_url='preview://light' if i%3 == 0 else None,
        team2_logo_url='preview://dark' if i%3 == 0 else None,
        scheduled_at=(now.replace(hour=10) + timedelta(minutes=i*30)).isoformat(), best_of=3)
        for i in range(20)]

    def save(name, data):
        image = Image.open(BytesIO(data)).convert('RGB') if isinstance(data, bytes) else data.convert('RGB')
        if image.height == 1080:
            draw = ImageDraw.Draw(image)
            draw.rectangle((38, 993, 1040, 1045), fill=m.NAVY)
            m._draw_text_block(draw, 540, 1018, 'МАКЕТ · ВЫМЫШЛЕННЫЕ ДАННЫЕ',
                              940, 22, display=True, fill=m.AMBER, max_lines=1)
        image.save(args.output / f'{name}.png')
        image.resize((360, round(image.height / 3)), Image.Resampling.LANCZOS).save(args.output / f'{name}-360.png')

    results = [MatchNormalized(source='pandascore', match_id=match.match_id,
        tournament_name=match.tournament_name, competition_key=match.competition_key,
        source_refs=match.source_refs, team1_name=match.team1_name, team2_name=match.team2_name,
        team1_logo_url=match.team1_logo_url, team2_logo_url=match.team2_logo_url,
        score1=2, score2=1, best_of=3, date=match.scheduled_at,
        maps=[MapResult(name=name, score1=13 if i != 1 else 9, score2=8 if i != 1 else 13)
              for i,name in enumerate(['Mirage','Nuke','Ancient'])]) for match in fixtures[:10]]
    for count in (1,4,10):
        for page, data in enumerate(m.render_schedule_cards(fixtures[:count],now,'Europe/Moscow'),1):
            save(f'schedule-{count}-{page}',data)
        for page,data in enumerate(m.render_results_cards(results[:count],now),1):
            save(f'digest-{count}-{page}',data)
    save('result-maps',m.render_result_card(results[0]))
    save('result-missing-maps',m.render_result_card(results[3].model_copy(update={'maps':[]})))
    save('context',m.render_schedule_context_covers(fixtures[:4],now)[0])
    mixed = [match.model_copy(update={'competition_key':f'Demo event {i%2}',
        'tournament_name':f'Demo event {i%2}', 'source_refs':None}) for i,match in enumerate(fixtures[:4])]
    save('schedule-mixed',m.render_schedule_card(mixed,now,'Europe/Moscow'))
    for count in (4,8,16):
        impacts = [TournamentVRSImpact(placement=str(i+1),team_name=pairs[i%5][0],team_id=str(i),
            before_points=1700,after_points=1700+(35 if i%2 == 0 else -15),
            before_rank=10+i,after_rank=8+i if i%2 == 0 else 13+i,
            points_delta=35 if i%2 == 0 else -15,rank_delta=2 if i%2 == 0 else -3,
            source='Valve VRS', before_version='demo-before',after_version='demo-after',
            before_effective_at='2026-09-21',after_effective_at='2026-09-30') for i in range(count)]
        for page,data in enumerate(m.render_tournament_vrs_cards('ESL Pro League Season 24',impacts,
            branding=profile.branding),1):
            save(f'vrs-{count}-{page}',data)
    scenes = r.storyboard(fixtures[:4])
    for i,scene in enumerate(scenes):
        save(f'reel-{i}', r.render_scene(scene,now,4,preview_watermark=True))
    names = ['context','schedule-4-1','digest-4-1','vrs-8-1','result-maps','result-missing-maps']
    sheet = Image.new('RGB',(1080,792),m.NAVY)
    draw = ImageDraw.Draw(sheet)
    for i,(name,label) in enumerate(zip(names,['Перед матчами','Расписание','Итоги дня','VRS','Результат: карты','Нет данных карт'])):
        x,y = i%3*360,i//3*396
        with Image.open(args.output/f'{name}-360.png') as im:
            sheet.paste(im,(x,y+36))
        draw.text((x+8,y+8),label,font=m._font(18,display=True),fill=m.WHITE)
    sheet.save(args.output/'series.png')
    if args.encode_reel:
        (args.output/'reel-demo.mp4').write_bytes(r.render_schedule_reel(fixtures[:4],now,
            preview_watermark=True))
    print(args.output.resolve())


if __name__ == '__main__':
    main()
