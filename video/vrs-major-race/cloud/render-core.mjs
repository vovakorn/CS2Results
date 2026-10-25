import {createCanvas,loadImage,GlobalFonts,Path2D} from '@napi-rs/canvas';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createHash} from 'node:crypto';
import {createPainter} from '../paint-frame.mjs';
import {FPS,frameCount} from '../timeline.mjs';
import {SAFE} from '../layout.mjs';
export const sha256=bytes=>createHash('sha256').update(bytes).digest('hex');

async function command(binary,args){
 const proc=spawn(binary,args,{stdio:['ignore','pipe','pipe']});let output='',error='';
 proc.stdout.on('data',c=>{output+=c.toString();if(output.length>200000)proc.kill();});
 proc.stderr.on('data',c=>{error+=c.toString();if(error.length>200000)proc.kill();});
 await new Promise((resolve,reject)=>{proc.on('error',reject);proc.on('close',code=>code===0?resolve():reject(Error('FFmpeg operation failed')));});
 return {output,error};
}

export async function renderVideo(data,directory,{ffmpeg,assetsDir}){
 if(!GlobalFonts.registerFromPath(path.join(assetsDir,'RussoOne-Regular.ttf'),'VRS Russo')||
    !GlobalFonts.registerFromPath(path.join(assetsDir,'Arimo-Bold.ttf'),'Arial'))throw Error('Missing bundled fonts');
 const projectLogo=await loadImage(path.join(assetsDir,'channel-logo.png'));
 const images=Object.fromEntries(await Promise.all(Object.entries(data.logos).map(async([name,src])=>[name,await loadImage(src)])));
 const paint=createPainter(data,{images,projectLogo,makeCanvas:createCanvas,makePath:d=>new Path2D(d)});
 const canvas=createCanvas(1080,1920),ctx=canvas.getContext('2d'),frames=frameCount(data);
 if(frames!==1944)throw Error('Unexpected timeline duration');
 const silent=path.join(directory,'silent.mp4'),file=path.join(directory,'reel.mp4');
 paint(ctx,17.1);await fs.writeFile(path.join(directory,'preview.png'),canvas.encodeSync('png'));
 const encoder=spawn(ffmpeg,['-y','-v','error','-f','rawvideo','-pix_fmt','rgba','-s:v','1080x1920','-r',String(FPS),'-i','pipe:0',
  '-an','-c:v','libx264','-threads','2','-preset','veryfast','-crf','17','-pix_fmt','yuv420p',
  '-vf','scale=out_color_matrix=bt709:out_range=tv','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-movflags','+faststart',silent],{stdio:['pipe','ignore','pipe']});
 let encoderError='';encoder.stderr.on('data',c=>{encoderError+=c.toString();if(encoderError.length>200000)encoder.kill();});
 const finished=new Promise((resolve,reject)=>{encoder.on('error',reject);encoder.on('close',code=>code===0?resolve():reject(Error('Video encoding failed')));});
 // Observe rejection even if a broken stdin interrupts the drawing loop first.
 finished.catch(()=>{});
 try{
  for(let frame=0;frame<frames;frame++){
   paint(ctx,frame/FPS);
   if(!encoder.stdin.write(canvas.data()))await once(encoder.stdin,'drain');
  }
  encoder.stdin.end();await finished;
 }catch(error){encoder.kill();throw error;}
 await command(ffmpeg,['-y','-v','error','-i',silent,'-i',path.join(assetsDir,'road-to-major.wav'),'-map','0:v:0','-map','1:a:0',
  '-c:v','copy','-c:a','aac','-b:a','192k','-ac','2','-ar','48000','-shortest','-movflags','+faststart',file]);
 const checked=await command(ffmpeg,['-v','info','-i',file,'-map','0:v:0','-map','0:a:0','-af','volumedetect','-progress','pipe:1','-f','null','-']);
 const decoded=Number([...checked.output.matchAll(/^frame=(\d+)/gm)].at(-1)?.[1]);
 const peak=Number(checked.error.match(/max_volume: ([\-\d.]+) dB/)?.[1]);
 if(decoded!==frames||!Number.isFinite(peak)||peak>=0||!checked.error.includes('1080x1920')||!checked.error.includes('Audio: aac'))throw Error('Encoded media validation failed');
 const bytes=await fs.readFile(file);
 const soundtrack=await fs.readFile(path.join(assetsDir,'road-to-major.wav'));
 const music=JSON.parse(await fs.readFile(path.join(assetsDir,'music-manifest.json'),'utf8'));
 if(sha256(soundtrack)!==music.sha256)throw Error('Soundtrack checksum mismatch');
 return {file,preview:path.join(directory,'preview.png'),sha256:sha256(bytes),bytes:bytes.length,
  width:1080,height:1920,fps:FPS,frames:decoded,duration:32.4,audioPeakDb:peak,musicSha256:music.sha256,fullDecode:'passed',
  layoutVersion:SAFE.version,coverOffsetMs:SAFE.coverOffsetMs};
}
