"""Audit decoded MP4 frames for transient loss of traces, table and moving logos."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
try:
    import numpy as np
except ImportError:
    bundle=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/lib'
    sys.path.insert(0,str(bundle/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'))
    import numpy as np
import imageio_ffmpeg

HERE=Path(__file__).resolve().parent
BASE=HERE.parents[1]/'output/vrs-major-race-2026-10-12/regional-reels'
parser=argparse.ArgumentParser()
parser.add_argument('--region',choices=['europe','americas','asia'])
parser.add_argument('--suffix',help='Video variant; defaults to the current regional manifest')
parser.add_argument('--video',type=Path,help='Explicit exported MP4, with --data and --region')
parser.add_argument('--data',type=Path,help='Regional data JSON corresponding to --video')
parser.add_argument('--allow-flashes',action='store_true')
args=parser.parse_args()
if args.video and (not args.data or not args.region):
    parser.error('--video requires --data and --region')
node=shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
reports={}
for slug in ([args.region] if args.region else ['europe','americas','asia']):
    suffix=args.suffix
    if not suffix and not args.video:
        manifest=json.loads((BASE/slug/'manifest.json').read_text())
        suffix=Path(manifest['file']).stem.removeprefix(f'vrs-{slug}-')
    path=args.video or BASE/slug/f'vrs-{slug}-{suffix}.mp4'
    data_path=args.data or HERE/'public/regional-data.json'
    reader=imageio_ffmpeg.read_frames(str(path),output_params=['-vf','scale=540:960'])
    meta=next(reader)
    assert meta['source_size']==(1080,1920),f"Unexpected video size: {meta['source_size']}"
    fps=meta['fps'];count=round(meta['duration']*fps)
    code=f'''import fs from 'node:fs';
import {{geometry}} from {json.dumps((HERE/'timeline.mjs').as_uri())};
const parsed=JSON.parse(fs.readFileSync({json.dumps(str(data_path))}));
const data=Array.isArray(parsed)?parsed.find(d=>d.regionSlug==={json.dumps(slug)}):parsed;
const frames=[];
for(let i=0;i<{count};i++){{const g=geometry(data,i/{fps});frames.push(g.endpoints.filter(e=>e.alpha>.98&&e.logoLayers.length===1).map(e=>[e.id,g.offsets.get(e.id)/2,e.y/2]));}}
process.stdout.write(JSON.stringify(frames));'''
    tracking=json.loads(subprocess.run([node,'--input-type=module','-e',code],check=True,capture_output=True,text=True).stdout)
    plot=[];table=[];logos={};decoded=0
    parsed=json.loads(data_path.read_text())
    source=next(r for r in parsed if r['regionSlug']==slug) if isinstance(parsed,list) else parsed
    step=source.get('stepSeconds',1.8)
    yy,xx=np.mgrid[-8:9,-8:9];circle=xx*xx+yy*yy<=64
    for frame,raw in enumerate(reader):
        im=np.frombuffer(raw,np.uint8).reshape((960,540,3))
        plot.append(float(np.max(im[215:801,56:337],axis=2).mean()))
        time=frame/fps
        anchor=min(len(source['snapshots'])-1,max(0,math.floor((time-1.8)/step+1e-8)))
        updating=anchor>0 and time-1.8-step*anchor<.25
        table.append(float('nan') if updating else float(np.max(im[209:808,342:488],axis=2).mean()))
        for identity,x,y in tracking[frame]:
            cx,cy=round(x),round(y)
            crop=im[cy-8:cy+9,cx-8:cx+9]
            if crop.shape[:2]!=(17,17):continue
            contrast=float(crop[circle].astype(np.float32).std(axis=0).mean())
            logos.setdefault(identity,np.full(count,np.nan))[frame]=contrast
        decoded+=1
    assert decoded==count,(decoded,count)
    flashes=[];table_flashes=[];logo_drops=[]
    for frame in range(4,count-4):
        local=min(np.mean(plot[frame-3:frame]),np.mean(plot[frame+1:frame+4]))
        dip=local-plot[frame]
        if dip>1.8:flashes.append({'frame':frame,'seconds':round(frame/fps,4),'brightnessDip':round(float(dip),4)})
        window=table[frame-3:frame+4]
        if np.isfinite(window).all():
            dip=min(np.mean(window[:3]),np.mean(window[4:]))-window[3]
            if dip>1.8:table_flashes.append({'frame':frame,'seconds':round(frame/fps,4),'brightnessDip':round(float(dip),4)})
    for identity,values in logos.items():
        for frame in range(4,count-4):
            neighbors=np.r_[values[frame-3:frame],values[frame+1:frame+4]]
            if not np.isfinite(values[frame]) or not np.isfinite(neighbors).all():continue
            baseline=min(np.mean(neighbors[:3]),np.mean(neighbors[3:]))
            if baseline>8 and values[frame]<baseline*.55:
                logo_drops.append({'frame':frame,'seconds':round(frame/fps,4),'teamCore':identity,
                                   'contrastRatio':round(float(values[frame]/baseline),4)})
    report={'file':path.name,'videoSha256':hashlib.sha256(path.read_bytes()).hexdigest(),'decodedFrames':decoded,'fps':fps,'plotFlashEvents':flashes,
            'logoDropEvents':logo_drops,'tableFlashEvents':table_flashes,'detector':{'plotDipThreshold':1.8,'logoContrastRatioThreshold':.55,
            'scope':'3 adjacent frames on either side; plot; table outside intended 0.25s data updates; visible unmixed moving logos; decoded pixels at 540x960'}}
    output=path.with_suffix('.pixel-audit.json') if args.video else BASE/slug/f'pixel-audit-{suffix}.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    reports[slug]=report
    print(json.dumps({'region':slug,'frames':decoded,'plotFlashes':len(flashes),'logoDrops':len(logo_drops),'tableFlashes':len(table_flashes)}))
if not args.allow_flashes and any(r['plotFlashEvents'] or r['logoDropEvents'] or r['tableFlashEvents'] for r in reports.values()):
    raise SystemExit('Decoded video contains a transient plot or logo loss')
