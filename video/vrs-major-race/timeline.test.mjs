import test from 'node:test';
import assert from 'node:assert/strict';
import {geometry, positions, sameCore, trace, duration, FPS, HOLD, STEP} from './timeline.mjs';
const row = (name,rank,points=100) => ({name,rank,points,roster:['a','b','c','d','e']});
const data = {names:['Falcons','Spirit'],snapshots:[
  {date:'2026-06-22',rows:[row('Falcons',2,1989.4),row('Spirit',1,1998.7)]},
  {date:'2026-07-06',rows:[row('Spirit',1,1992.4),row('Falcons',6,1842.8)]}]};
test('cohort position and global rank remain different measures',()=>{
  assert.deepEqual(positions(data.snapshots[1],data.names),{Spirit:1,Falcons:2});
  assert.equal(data.snapshots[1].rows[1].rank,6);
});
test('transition labels point to the previous actual observation',()=>{
  const g=geometry(data,2.7);
  assert.equal(g.i,0);assert.ok(Math.abs(g.fraction-.5)<1e-8);
  assert.equal(g.row(g.i,'Falcons').points,1989.4);
  assert.ok(Math.abs(g.day-7)<1e-8);
});
test('missing observation leaves a gap instead of becoming a zero',()=>{
  const missing=structuredClone(data);
  missing.snapshots[0].rows=[row('Spirit',1)];
  const g=geometry(missing,2.7);
  assert.equal(g.row(0,'Falcons'),undefined);
  assert.equal(trace(g,'Falcons'),'');
  const entering=g.endpoints.find((e)=>e.name==='Falcons');
  assert.ok(Math.abs(entering.alpha-.5)<1e-8);
  assert.equal(entering.row.points,1842.8);
});
test('an overtake has no one-frame lane switch at an anchor',()=>{
  const swapping=structuredClone(data);
  swapping.snapshots[1].rows=[row('Falcons',1),row('Spirit',2)];
  const before=geometry(swapping,HOLD+STEP-1e-6);
  const after=geometry(swapping,HOLD+STEP+1e-6);
  for(const head of before.endpoints){
    const next=after.endpoints.find((e)=>e.id===head.id);
    assert.ok(Math.abs(before.offsets.get(head.id)-after.offsets.get(next.id))<.001);
    assert.ok(Math.abs(head.y-next.y)<.001);
  }
});
test('logos remain separated while overtaking and move continuously',()=>{
  const swapping=structuredClone(data);
  swapping.snapshots[1].rows=[row('Falcons',1),row('Spirit',2)];
  let previous;
  for(let frame=0;frame<Math.round(duration(swapping)*FPS);frame++){
    const g=geometry(swapping,frame/FPS),[a,b]=g.endpoints;
    assert.ok(Math.hypot(g.offsets.get(a.id)-g.offsets.get(b.id),a.y-b.y)>=49);
    if(previous)for(const e of g.endpoints){
      assert.ok(Math.abs(g.offsets.get(e.id)-previous.offsets.get(e.id))<12);
    }
    previous=g;
  }
});
test('cubic trail finishes exactly at the moving observation cursor',()=>{
  const swapping=structuredClone(data);
  swapping.snapshots[1].rows=[row('Falcons',1),row('Spirit',2)];
  const g=geometry(swapping,2.7);
  const end=trace(g,'Falcons').trim().match(/([-\d.]+) ([-\d.]+)$/);
  const head=g.endpoints.find((e)=>e.name==='Falcons');
  assert.ok(Math.abs(Number(end[1])-head.x)<.001);
  assert.ok(Math.abs(Number(end[2])-head.y)<.001);
});
test('missing or replaced cores fade and keep a real trace gap',()=>{
  const changed=structuredClone(data);
  changed.snapshots[1].rows[1].roster=['a','b','x','y','z'];
  const g=geometry(changed,2.7);
  const heads=g.endpoints.filter((e)=>e.name==='Falcons');
  assert.equal(heads.length,2);
  assert.ok(heads.every((e)=>Math.abs(e.alpha-.5)<1e-8));
  assert.equal(trace(g,'Falcons'),'');
});
test('labels finish the final snapshot fade during the final hold',()=>{
  assert.equal(geometry(data,duration(data)).labelMix,1);
});
test('fewer than three distinct shared players breaks the trace',()=>{
  const changed=structuredClone(data);
  changed.snapshots[1].rows[1].roster=['a','b','x','y','z'];
  assert.equal(sameCore(changed.snapshots[0].rows[0],changed.snapshots[1].rows[1]),false);
  assert.equal(trace(geometry(changed,3.6),'Falcons'),'');
});
test('final frame is the final real snapshot',()=>{
  const g=geometry(data,duration(data));
  assert.equal(g.i,1);assert.equal(g.fraction,0);assert.ok(Math.abs(g.left)<1e-8);
  assert.equal(g.row(g.i,'Falcons').points,1842.8);
});
