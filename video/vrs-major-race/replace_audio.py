"""Replace the soundtrack while preserving the verified encoded video exactly."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import imageio_ffmpeg
from music import PUBLIC, render, signature

HERE=Path(__file__).resolve().parent
BASE=HERE.parents[1]/'output/vrs-major-race-2026-10-12/regional-reels'
slugs=['europe','americas','asia']
manifests={slug:json.loads((BASE/slug/'manifest.json').read_text()) for slug in slugs}
durations={m['duration'] for m in manifests.values()}
assert len(durations)==1,'Regions must have the same duration'
duration=durations.pop()
marker=PUBLIC/'music-signature.txt'
if not (PUBLIC/'original-soundtrack.wav').exists() or not marker.exists() or marker.read_text()!=signature(duration):
    render(duration)
audio=PUBLIC/'original-soundtrack.wav'
music=json.loads((PUBLIC/'music-manifest.json').read_text())
variant=music.get('variant','custom-music')
assert re.fullmatch('[a-z0-9-]+',variant)
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
assert sha(audio)==music['sha256']
ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
report=json.loads((BASE/'validation.json').read_text())
archive=Path(manifests['americas']['file']).stem.removeprefix('vrs-americas-')
if archive!=variant and not (BASE/f'validation-{archive}.json').exists():
    shutil.copyfile(BASE/'validation.json',BASE/f'validation-{archive}.json')
if archive!=variant and not (BASE/f'music-manifest-{archive}.json').exists():
    shutil.copyfile(BASE/'music-manifest.json',BASE/f'music-manifest-{archive}.json')

def video_hash(path):
    result=subprocess.run([ffmpeg,'-v','error','-i',str(path),'-map','0:v:0','-c:v','copy',
                           '-f','hash','-'],check=True,capture_output=True,text=True)
    return result.stdout.strip()

updates={}
for slug in slugs:
    directory=BASE/slug
    manifest=manifests[slug]
    source=directory/f'vrs-{slug}.mp4'
    source_stream=video_hash(source)
    source_sha=sha(source)
    previous=Path(manifest['file']).stem.removeprefix(f'vrs-{slug}-')
    if previous!=variant and not (directory/f'manifest-{previous}.json').exists():
        shutil.copyfile(directory/'manifest.json',directory/f'manifest-{previous}.json')
    filename=f'vrs-{slug}-{variant}.mp4'
    result=directory/filename
    subprocess.run([ffmpeg,'-y','-v','error','-i',str(source),'-i',str(audio),
        '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',
        '-ac','2','-ar','48000','-shortest','-movflags','+faststart',str(result)],check=True)
    assert video_hash(result)==source_stream,'Video stream changed'
    checked=subprocess.run([ffmpeg,'-v','info','-i',str(result),'-map','0:v:0','-map','0:a:0',
        '-af','volumedetect','-f','null','-'],check=True,capture_output=True,text=True)
    peak=float(re.search(r'max_volume: ([\-\d.]+) dB',checked.stderr).group(1))
    assert peak<0,'Audio clipping'
    reader=imageio_ffmpeg.read_frames(str(result));meta=next(reader);reader.close()
    assert meta['source_size']==(1080,1920) and meta['fps']==manifest['fps'] and meta['audio_codec']=='aac'
    assert abs(meta['duration']-duration)<.04
    subprocess.run([sys.executable,str(HERE/'check-video-pixels.py'),'--region',slug,'--suffix',variant],check=True)
    raster=json.loads((directory/f'pixel-audit-{variant}.json').read_text())
    assert raster['decodedFrames']==manifest['frames']
    unchanged=manifest.get('musicSha256')==music['sha256']
    manifest.update(file=filename,currentFile=source.name,soundtrack=music['label'],
                    musicReference=music['reference'],musicSha256=music['sha256'],
                    rasterValidation=f'pixel-audit-{variant}.json')
    report['regions'][slug]['video'].update(file=filename,bytes=result.stat().st_size,
        sha256=sha(result),audioPeakDb=peak,fullDecode='passed',videoStreamUnchanged=True,
        soundtrackUnchanged=unchanged,videoStreamSha256=source_stream,
        previousMp4Sha256=source_sha)
    report['regions'][slug]['rasterAudit']={
        'report':f'{slug}/pixel-audit-{variant}.json','frames':raster['decodedFrames'],
        'plotFlashEvents':len(raster['plotFlashEvents']),'logoDropEvents':len(raster['logoDropEvents']),
        'tableFlashEvents':len(raster['tableFlashEvents']),'scope':raster['detector']['scope']}
    updates[slug]=(result,source,manifest)
    print(f'{slug}: audio checked; {duration}s; unchanged video stream; peak {peak} dB')
# Promote only after every region has passed both audio and raster checks.
for slug,(result,source,manifest) in updates.items():
    shutil.copyfile(result,source)
    (BASE/slug/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
shutil.copyfile(audio,BASE/f'race-{variant}.wav')
subprocess.run([ffmpeg,'-y','-v','error','-i',str(audio),'-c:a','libmp3lame',
                '-q:a','2',str(BASE/f'race-{variant}.mp3')],check=True)
shutil.copyfile(PUBLIC/'music-manifest.json',BASE/'music-manifest.json')
report['music']=music
report['validatedOn']='2026-10-16'
(BASE/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(BASE/f'race-{variant}.wav')
