import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {createCanvas,loadImage,GlobalFonts,Path2D} from '@napi-rs/canvas';
import {createPainter} from '../paint-frame.mjs';
import {prepareMonthlyWorld} from '../monthly.mjs';
import {prepareRegions} from '../data.mjs';
import {SAFE} from '../layout.mjs';
import {geometry} from '../timeline.mjs';

const raw=JSON.parse(gunzipSync(fs.readFileSync(new URL('../fixtures/liquipedia-history.json.gz',import.meta.url))));
GlobalFonts.registerFromPath(new URL('../../../cs2bot/assets/fonts/RussoOne-Regular.ttf',import.meta.url).pathname,'VRS Russo');
GlobalFonts.registerFromPath(new URL('../assets/Arimo-Bold.ttf',import.meta.url).pathname,'Arial');
const projectLogo=await loadImage(new URL('../../../cs2bot/assets/channel-logo.png',import.meta.url).pathname);
const inside=(b,bounds=[SAFE.left,SAFE.top,SAFE.right,SAFE.bottom])=>
 b[0]>=bounds[0]-.01&&b[1]>=bounds[1]-.01&&b[2]<=bounds[2]+.01&&b[3]<=bounds[3]+.01;

// Carry measured text bounds through the same cached canvases as real painting.
function recorder(){
 const labels=new WeakMap();
 function makeCanvas(w,h){
  const canvas=createCanvas(w,h),ctx=canvas.getContext('2d'),items=[];
  labels.set(canvas,items);
  const fill=ctx.fillText.bind(ctx),draw=ctx.drawImage.bind(ctx);
  ctx.fillText=(value,x,y,...rest)=>{
   if(ctx.globalAlpha>0){
    const m=ctx.measureText(value);
    items.push({value,box:[x-m.actualBoundingBoxLeft,y-m.actualBoundingBoxAscent,
                         x+m.actualBoundingBoxRight,y+m.actualBoundingBoxDescent]});
   }
   return fill(value,x,y,...rest);
  };
  ctx.drawImage=(source,...args)=>{
   if(ctx.globalAlpha>0&&labels.has(source)){
    const [sx,sy,sw,sh,dx,dy,dw,dh]=args.length===8?args:
     [0,0,source.width,source.height,...args.slice(0,2),...args.length===4?args.slice(2):[source.width,source.height]];
    for(const {value,box} of labels.get(source))items.push({value,
     box:[dx+(box[0]-sx)*dw/sw,dy+(box[1]-sy)*dh/sh,
          dx+(box[2]-sx)*dw/sw,dy+(box[3]-sy)*dh/sh]});
   }
   return draw(source,...args);
  };
  return canvas;
 }
 return {makeCanvas,labels};
}

test('world and all regional frames keep measured labels inside safe bounds; cover survives crops',async()=>{
 const datasets=[prepareMonthlyWorld(raw,{month:'2026-09',collectedOn:'2026-10-16'}),
  ...prepareRegions(raw,{collectedOn:'2026-10-16',through:'2026-10-16'})];
 for(const data of datasets){
  const {makeCanvas,labels}=recorder();
  const images=Object.fromEntries(await Promise.all(Object.entries(data.logos).map(async([k,v])=>[k,await loadImage(v)])));
  const paint=createPainter(data,{makeCanvas,makePath:d=>new Path2D(d),images,projectLogo});
  const canvas=makeCanvas(1080,1920),ctx=canvas.getContext('2d');
  for(const seconds of [0,.5,1.2,1.4,1.6,1.8,2.3,17.1,32,32.383]){
   const g=geometry(data,seconds),radius=data.names.length>20?18:21;
   assert.ok(g.endpoints.every(e=>inside([g.offsets.get(e.id)-radius,e.y-radius,
                                        g.offsets.get(e.id)+radius,e.y+radius])));
   const boxes=labels.get(canvas);boxes.length=0;paint(ctx,seconds);
   assert.ok(boxes.length);
   assert.deepEqual(boxes.filter(v=>!inside(v.box)),[],`${data.regionSlug} @ ${seconds}s`);
   if(seconds===.5){
    for(const value of data.headline?['VRS · ИТОГИ','МЕСЯЦА']:['ДОРОГА К','MAJOR']){
     const title=boxes.find(v=>v.value===value);assert.ok(title);
     assert.ok(inside(title.box,[80,420,920,1500]),value);
    }
   }
  }
 }
});
