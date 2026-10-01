"""Offline review fixtures. No accounts, uploads, or remote logos."""
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
import argparse, sys, json, hashlib
from PIL import Image, ImageDraw
parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--output-dir',type=Path,default=Path('build/typography-review'));parser.add_argument('--phase',choices=['before','after'],default='after');parser.add_argument('--encode-reel',action='store_true');args=parser.parse_args()
sys.path.insert(0,str(args.source_root))
from cs2bot import media_cards as m, schedule_reels as r
from cs2bot.match_sources.models import UpcomingMatchNormalized as U, MatchNormalized as R, MapResult, TournamentPlacement as P, TournamentVRSImpact as V, TournamentRadar, RadarBracketMatch, RadarBracketNode
out=args.output_dir/args.phase;out.mkdir(parents=True,exist_ok=True)
now=datetime(2026,10,1,9,tzinfo=ZoneInfo('Europe/Moscow'))
pairs=[('NAVI','MOUZ'),('Team Spirit','Team Vitality'),('Gaimin Gladiators Academy','Natus Vincere Junior'),('Complexity Gaming','The MongolZ'),('G2 Esports','FaZe Clan'),('Team Falcons','Eternal Fire'),('Virtus.pro','Aurora Gaming'),('FURIA','GamerLegion'),('3DMAX','HEROIC'),('Astralis','Team Liquid')]
def logo(url,**kwargs):
 if not url or not url.startswith('preview://'):return None
 im=Image.new('RGBA',(200,200));d=ImageDraw.Draw(im);light=url.endswith('light'); c=(250,250,250) if light else (6,10,18)
 d.polygon([(100,12),(188,174),(12,174)],fill=c);d.ellipse((75,70,125,120),fill=(22,199,255));return im
m.fetch_team_logo=logo
fixtures=[]
for i in range(20):
 a,b=pairs[i%10]
 fixtures.append(U(match_id=f'u{i}',competition_key='BLAST Open Porto 2026',tournament_name='BLAST Open Porto 2026',team1_name=a,team2_name=b,scheduled_at=(now.replace(hour=10)+timedelta(minutes=30*i)).isoformat(),best_of=3,team1_logo_url='preview://light' if i%3==0 else None,team2_logo_url='preview://dark' if i%3==0 else None))
results=[R(source='pandascore',match_id=f'r{i}',competition_key=x.competition_key,tournament_name=x.tournament_name,team1_name=x.team1_name,team2_name=x.team2_name,team1_logo_url=x.team1_logo_url,team2_logo_url=x.team2_logo_url,score1=2 if i%2==0 else 1,score2=1 if i%2==0 else 2,best_of=3,date=x.scheduled_at) for i,x in enumerate(fixtures[:10])]
manifest={}
def save(name,data):
 im=Image.open(BytesIO(data)).convert('RGB') if isinstance(data,bytes) else data.convert('RGB');im.save(out/(name+'.png'))
 for size in (360,180): im.resize((size,round(size*im.height/im.width)),Image.Resampling.LANCZOS).save(out/f'{name}-{size}.png')
 manifest[name]={'size':list(im.size)}
for n in (1,4,10,20):
 cards=m.render_schedule_cards(fixtures[:n],now,'Europe/Moscow')
 for i,b in enumerate(cards):save(f'schedule-{n}-p{i+1}',b)
for n in (1,4,10):
 cards=m.render_results_cards(results[:n],now) if hasattr(m,'render_results_cards') else [m.render_results_card(results[:n],now)]
 for i,b in enumerate(cards):save(f'digest-{n}-p{i+1}',b)
save('result-single',m.render_result_card(results[2]))
for count in (3,4,5):
 score=(2,1) if count==3 else (3,count-3)
 maps=[MapResult(name=name,score1=13 if i<score[0] else 9,score2=9 if i<score[0] else 13) for i,name in enumerate(['Mirage','Dust II','Nuke','Ancient','Inferno'][:count])]
 x=results[0].model_copy(update={'source':'liquipedia','is_final':True,'score1':score[0],'score2':score[1],'maps':maps,'winner_prize_usd':500000})
 save(f'final-{count}',m.render_final_card(x))
placements=[P(placement='1' if i==0 else '2' if i==1 else '3–4' if i<4 else '5–8',team_name=pairs[i%10][0],prize_usd=500000//(i+1)) for i in range(8)]
save('standings',m.render_tournament_standings_cards('BLAST Open Porto 2026',placements)[0])
impacts=[V(placement=str(i+1),team_name=pairs[i][0],team_id=str(i),before_points=1000,after_points=1000+(35 if i%3==0 else -15 if i%3==1 else 0),before_rank=i+3,after_rank=i+1,points_delta=35 if i%3==0 else -15 if i%3==1 else 0,rank_delta=2 if i%3==0 else -3 if i%3==1 else 0,source='Valve',before_version='before',after_version='after',before_effective_at='2026-09-21',after_effective_at='2026-09-30') for i in range(8)]
save('vrs',m.render_tournament_vrs_cards('BLAST Open Porto 2026',impacts)[0])
radar=TournamentRadar(tournament_id='t',next_matches=fixtures[:1],roster_team_count=16,bracket_match_count=8)
save('radar-next',m.render_tournament_radar_card(radar,'BLAST Open Porto 2026','Europe/Moscow','next_match'))
opening=[RadarBracketMatch(match_id=f'b{i}',round_name='Opening round',team1_name=a,team2_name=b) for i,(a,b) in enumerate(pairs[:4])]
linked=[*opening,RadarBracketNode(match_id='b4',round_name='Semifinal',previous_match_ids=['b0','b1']),RadarBracketNode(match_id='b5',round_name='Semifinal',previous_match_ids=['b2','b3']),RadarBracketNode(match_id='b6',round_name='Final',previous_match_ids=['b4','b5'])]
for kind,nodes in [('pairs',opening),('linked',linked)]:
 radar=TournamentRadar(tournament_id='t',bracket_matches=opening,bracket_structure=nodes if kind=='linked' else [],roster_team_count=8,bracket_match_count=len(nodes))
 for i,b in enumerate(m.render_tournament_radar_cards(radar,'BLAST Open Porto 2026','Europe/Moscow','bracket')):save(f'radar-{kind}-p{i+1}',b)
for i,b in enumerate(m.render_schedule_context_covers(fixtures[:4],now)):save(f'context-p{i+1}',b)
long=fixtures[2].model_copy(update={'tournament_name':'FISSURE Playground — Проверка Ёжика и длинного названия турнира — Group A','competition_key':None})
for i,b in enumerate(m.render_schedule_cards([long],now,'Europe/Moscow')):save(f'long-event-p{i+1}',b)
for n in (1,4,10,20):
 for i,scene in enumerate(r.storyboard(fixtures[:n])):save(f'reel-{n}-scene{i}',r.render_scene(scene,now,n,preview_watermark=True))
if args.phase == 'after':
 # Maximum supported structures and deliberately interleaved tournaments.
 nodes=[RadarBracketNode(match_id=f'dense-{i}', round_name='Semifinal' if i%6 else 'Opening round', team1_name=pairs[i%10][0], team2_name=pairs[i%10][1], previous_match_ids=[] if i==0 else [f'dense-{i-1}'], status='running' if i==47 else None) for i in range(48)]
 nodes[-1]=nodes[-1].model_copy(update={'previous_match_ids':['dense-0','dense-46']})
 dense=TournamentRadar(tournament_id='dense',bracket_structure=nodes,roster_team_count=64,bracket_match_count=48)
 for i,b in enumerate(m.render_tournament_radar_cards(dense,'BLAST Open Porto 2026','Europe/Moscow','bracket')):save(f'radar-48-p{i+1}',b)
 for i,b in enumerate(m.render_tournament_standings_cards('BLAST Open Porto 2026',[placements[i%8].model_copy(update={'placement':str(i+1)}) for i in range(64)])):save(f'standings-64-p{i+1}',b)
 for i,b in enumerate(m.render_tournament_vrs_cards('BLAST Open Porto 2026',[impacts[i%8].model_copy(update={'placement':str(i+1),'team_id':str(i)}) for i in range(64)])):save(f'vrs-64-p{i+1}',b)
 mixed=[x.model_copy(update={'competition_key':f'Event {i%3}','tournament_name':f'IEM / BLAST EVENT {i%3}'}) for i,x in enumerate(fixtures)]
 for i,b in enumerate(m.render_schedule_cards(mixed,now,'Europe/Moscow')):save(f'schedule-mixed-20-p{i+1}',b)
 save('final-long',m.render_final_card(results[2].model_copy(update={'source':'liquipedia','is_final':True,'maps':[MapResult(name=n,score1=a,score2=b) for n,a,b in [('Mirage',13,9),('Nuke',9,13),('Ancient',13,8)]],'winner_prize_usd':500000})))
 save('standings-max-prize',m.render_tournament_standings_cards('BLAST Open Porto 2026',[P(placement=str(i+1),team_name=pairs[i][0],prize_usd=10_000_000_000) for i in range(8)])[0])
if args.encode_reel:
 video=r.render_schedule_reel(fixtures,now,'Europe/Moscow',preview_watermark=True)
 (out/'schedule-reel-20.mp4').write_bytes(video)
 manifest['reel_video']={'file':'schedule-reel-20.mp4','bytes':len(video),'storyboard_seconds':sum(x.duration for x in r.storyboard(fixtures))}
if args.phase == 'after':
 for label,url,tournament in [('event-logo','preview://light','BLAST Open Porto 2026'),('event-logo-long','preview://dark','FISSURE Playground — Проверка Ёжика и длинного названия турнира'),('event-logo-missing','preview://missing','BLAST Open Porto 2026')]:
  # Missing logo deliberately uses a loader miss to verify centered text fallback.
  members=[x.model_copy(update={'tournament_logo_url':url if label!='event-logo-missing' else 'offline://missing','tournament_name':tournament,'competition_key':None}) for x in fixtures[:4]]
  save(f'schedule-{label}',m.render_schedule_cards(members,now,'Europe/Moscow')[0])
 save('context-single',m.render_schedule_context_covers([fixtures[2]],now)[0])
 save('final-long-event',m.render_final_card(results[2].model_copy(update={'source':'liquipedia','is_final':True,'tournament_name':long.tournament_name,'score1':3,'score2':2,'maps':[MapResult(name=n,score1=a,score2=b) for n,a,b in [('Mirage',13,9),('Dust II',13,9),('Nuke',9,13),('Ancient',9,13),('Inferno',13,8)]],'winner_prize_usd':500000})))
manifest['sources']={str(p.relative_to(args.source_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [args.source_root/'cs2bot/media_cards.py',args.source_root/'cs2bot/schedule_reels.py']}
(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));print(args.phase,len(manifest)-1,'images',out)
