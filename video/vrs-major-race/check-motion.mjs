import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';
import {geometry,frameCount,FPS,HOLD,STEP,trace} from './timeline.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
const data=JSON.parse(fs.readFileSync(path.join(here,'public/regional-data.json')));
const report={fps:FPS,regions:{}};
for(const region of data){
 let previous,maxDx=0,maxDy=0,overlaps=0;
 const diameter=region.names.length>20?42:49;
 for(let frame=0;frame<frameCount(region);frame++){
  const g=geometry(region,frame/FPS);
  for(const head of g.endpoints){
   assert.ok(head.alpha>=0&&head.alpha<=1);
   assert.ok(g.offsets.get(head.id)>=112-1e-6 && g.offsets.get(head.id)<=662+1e-6);
   const before=previous?.endpoints.find(e=>e.id===head.id);
   if(before){
    maxDx=Math.max(maxDx,Math.abs(g.offsets.get(head.id)-previous.offsets.get(head.id)));
    maxDy=Math.max(maxDy,Math.abs(head.y-before.y));
   }
  }
  for(let a=0;a<g.endpoints.length;a++)for(let b=a+1;b<g.endpoints.length;b++){
   const p=g.endpoints[a],q=g.endpoints[b];
   if(Math.min(p.alpha,q.alpha)<.25)continue;
   if(Math.hypot(g.offsets.get(p.id)-g.offsets.get(q.id),p.y-q.y)<diameter-1e-6)overlaps++;
  }
  for(const name of region.names)assert.ok(!/NaN|Infinity/.test(trace(g,name)));
  previous=g;
 }
 let boundaryJump=0;
 for(let anchor=1;anchor<region.snapshots.length;anchor++){
  const at=HOLD+STEP*anchor;
  const a=geometry(region,at-1e-6),b=geometry(region,at+1e-6);
  for(const head of a.endpoints){
   const after=b.endpoints.find(e=>e.id===head.id);
   if(after&&Math.min(head.alpha,after.alpha)>.01)
    boundaryJump=Math.max(boundaryJump,Math.hypot(a.offsets.get(head.id)-b.offsets.get(head.id),head.y-after.y));
  }
 }
 assert.equal(overlaps,0);
 assert.ok(maxDx<12,`Horizontal frame jump ${region.regionSlug}: ${maxDx}`);
 assert.ok(boundaryJump<.01,`Snapshot boundary jump: ${boundaryJump}`);
 report.regions[region.regionSlug]={frames:frameCount(region),maxHorizontalPixelsPerFrame:maxDx,
  maxVerticalPixelsPerFrame:maxDy,maxAnchorBoundaryJumpPixels:boundaryJump,visibleBadgeOverlaps:overlaps,
  finiteCubicTrails:true};
}
fs.writeFileSync(path.resolve(here,'../../output/vrs-major-race-2026-10-12/regional-reels/motion-validation.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify(report));
