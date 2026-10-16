import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs/promises';
import {spawn,spawnSync} from 'node:child_process';
import {once} from 'node:events';
import {createPainter} from './paint-frame.mjs';
import {FPS,frameCount} from './timeline.mjs';
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(here,'../..');
const require=createRequire(import.meta.url);
let canvasLib;
try{canvasLib=require('@napi-rs/canvas');}catch{
 const bundled=createRequire(path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/entry.cjs'));
 canvasLib=bundled('@napi-rs/canvas');
}
const {createCanvas,loadImage,GlobalFonts,Path2D}=canvasLib;
GlobalFonts.registerFromPath(path.join(root,'cs2bot/assets/fonts/RussoOne-Regular.ttf'),'VRS Russo');
GlobalFonts.registerFromPath('/System/Library/Fonts/Supplemental/Arial.ttf','Arial');
GlobalFonts.registerFromPath('/System/Library/Fonts/Supplemental/Arial Bold.ttf','Arial');
const python=path.join(root,'.venv/bin/python');
const prep=spawnSync(python,[path.join(here,'prepare.py')],{encoding:'utf8'});
if(prep.status!==0)throw Error(prep.stderr);console.log(prep.stdout.trim());
const regions=JSON.parse(await fs.readFile(path.join(here,'public/regional-data.json'),'utf8'));
const music=JSON.parse(await fs.readFile(path.join(here,'public/music-manifest.json'),'utf8'));
const variant=music.variant||'stable';
if(!/^[a-z0-9-]+$/.test(variant))throw Error('Invalid soundtrack variant');
const requested=process.argv.includes('--region')?process.argv[process.argv.indexOf('--region')+1]?.split(','):null;
if(requested&&requested.some(s=>!regions.some(r=>r.regionSlug===s)))throw Error('Unknown region');
const ffmpegCall=spawnSync(python,['-c','import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'],{encoding:'utf8'});
if(ffmpegCall.status!==0)throw Error(ffmpegCall.stderr);const ffmpeg=ffmpegCall.stdout.trim();
const outputRoot=path.join(root,'output/vrs-major-race-2026-10-12/regional-reels');
const projectLogo=await loadImage(path.join(here,'public/channel-logo.png'));
for(const data of regions.filter(r=>!requested||requested.includes(r.regionSlug))){
 const slug=data.regionSlug,output=path.join(outputRoot,slug);await fs.mkdir(output,{recursive:true});
 const images=Object.fromEntries(await Promise.all(Object.entries(data.logos).map(async([name,src])=>[name,await loadImage(src)])));
 const paint=createPainter(data,{images,projectLogo,makeCanvas:createCanvas,makePath:d=>new Path2D(d)});
 const canvas=createCanvas(1080,1920),ctx=canvas.getContext('2d'),frames=frameCount(data);
 for(const [name,frame] of [['start',Math.round(1.4*FPS)],['middle',Math.floor(frames/2)],['crossing',Math.round(17.1*FPS)],['final',frames-FPS],['cover',frames-1]]){
  paint(ctx,frame/FPS);await fs.writeFile(path.join(output,`${name}.png`),canvas.encodeSync('png'));
 }
 console.log(`${slug}: atomic canvas stills rendered`);
 if(process.argv.includes('--stills-only'))continue;
 const silentFile=`vrs-${slug}-silent.mp4`,silent=path.join(output,silentFile);
 const encoder=spawn(ffmpeg,['-y','-v','error','-f','rawvideo','-pix_fmt','rgba','-s:v','1080x1920','-r',String(FPS),'-i','pipe:0',
  '-an','-c:v','libx264','-preset','veryfast','-crf','17','-pix_fmt','yuv420p',
  '-vf','scale=out_color_matrix=bt709:out_range=tv','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-movflags','+faststart',silent],{stdio:['pipe','ignore','pipe']});
 let stderr='';encoder.stderr.on('data',chunk=>{stderr+=chunk.toString();});
 const finished=new Promise((resolve,reject)=>{encoder.on('error',reject);encoder.on('close',code=>code===0?resolve():reject(Error(stderr)));});
 let last=-1;
 for(let frame=0;frame<frames;frame++){
  paint(ctx,frame/FPS);
  if(!encoder.stdin.write(canvas.data()))await once(encoder.stdin,'drain');
  const step=Math.floor((frame+1)/frames*10);if(step!==last){last=step;console.log(`${slug}: ${step*10}%`);}
 }
 encoder.stdin.end();await finished;
 const filename=`vrs-${slug}-${variant}.mp4`;
 const mux=spawnSync(ffmpeg,['-y','-v','error','-i',silent,'-i',path.join(here,'public/original-soundtrack.wav'),
  '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-ac','2','-ar','48000','-shortest','-movflags','+faststart',path.join(output,filename)],{encoding:'utf8'});
 if(mux.status!==0)throw Error(mux.stderr);
 const check=spawnSync(python,[path.join(here,'check-video-pixels.py'),'--region',slug,'--suffix',variant],{encoding:'utf8'});
 console.log(check.stdout.trim());if(check.status!==0)throw Error(check.stderr);
 await fs.copyFile(path.join(output,filename),path.join(output,`vrs-${slug}.mp4`));
 await fs.writeFile(path.join(output,'manifest.json'),JSON.stringify({renderer:'Atomic CPU Canvas / Skia; shared painter with Remotion React preview',
  reactComponent:'video/vrs-major-race/VRSReel.jsx',sharedPainter:'video/vrs-major-race/paint-frame.mjs',file:filename,silentFile,
  width:1080,height:1920,fps:FPS,frames,duration:frames/FPS,source:'LiquipediaDB vrs_ranking_live',collectedOn:data.collectedOn,
  sourceDates:data.snapshots.map(s=>s.date),region:data.regionLabel,quota:data.quota,cohort:data.names,selection:data.selection,
  verticalAxis:`Ordinal position among the ${data.names.length} displayed histories`,rightRank:data.rankDerivation,eligibility:data.eligibility,
  numericLabels:'Exact observed values; date and fixed table slots crossfade together for 0.22s; missing observations are dashes',
  duplicateChoices:data.duplicateChoices,brandBridges:data.brandBridges,dataSha256:data.inputSha256,
  visualReference:'https://www.instagram.com/reel/DeXDGhdAPvT/',style:'Project logo, gradient header; no snapshot counter or footer text',
  animation:'Continuous crossing plans; exact cubic trails; atomic complete frames; cached decoded sprites and table textures',
  rasterValidation:`pixel-audit-${variant}.json`,soundtrack:music.label,musicReference:music.reference,musicSha256:music.sha256,
  status:'Local artifact; invitations are not confirmed'},null,2));
 console.log(path.join(output,filename));
}
