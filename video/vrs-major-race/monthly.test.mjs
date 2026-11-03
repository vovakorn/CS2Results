import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {monthlyPeriod,prepareMonthlyWorld,monthlyCaptionFor} from './monthly.mjs';
import {geometry,frameCount,trace,boundaryTrace} from './timeline.mjs';
const fixture=()=>JSON.parse(gunzipSync(fs.readFileSync(new URL('./fixtures/liquipedia-history.json.gz',import.meta.url))));
const options={month:'2026-09',collectedOn:'2026-10-16'};
test('calendar boundaries handle leap years, December and reject open/invalid months',()=>{
 assert.equal(monthlyPeriod('2028-02','2028-03-01').end,'2028-02-29');
 assert.equal(monthlyPeriod('2026-12','2027-01-01').end,'2026-12-31');
 assert.equal(monthlyPeriod('2027-01','2027-02-01').from,'2026-12-24');
 for(const month of ['2026-10','2026-11','2026-13','2026-00','2026-9','../'])assert.throws(()=>monthlyPeriod(month,'2026-10-16'));
});
test('one world top-20 uses a real baseline and no rankings outside the reporting month',()=>{
 const data=prepareMonthlyWorld(fixture(),options);
 assert.equal(data.names.length,25);assert.equal(data.regionSlug,'world');
 assert.equal(data.headline,'CS2 · МИРОВОЙ РЕЙТИНГ');
 assert.equal(data.hookPeriod,'СЕНТЯБРЬ 2026 · МИРОВОЙ ТОП-20');
 assert.deepEqual(data.snapshots.map(s=>s.date),['2026-08-31','2026-09-07','2026-09-14','2026-09-21','2026-09-28']);
 assert.deepEqual(data.snapshots.at(-1).rows.map(r=>r.rank),Array.from({length:20},(_,i)=>i+1));
 assert.equal(frameCount(data),1320);
 assert.equal(monthlyCaptionFor(data),'VRS — итоги сентября 2026\nКак менялся мировой топ-20 за месяц\n\nИсточник: LiquipediaDB.');
 assert.ok(!monthlyCaptionFor(data).includes('#Major'));
});
test('every dated ranking contains exactly top 20 and the viewport never extends below place 20',()=>{
 const data=prepareMonthlyWorld(fixture(),options),g=geometry(data,0);
 for(const snapshot of data.snapshots){
  assert.equal(snapshot.rows.length,20);
  assert.deepEqual(snapshot.rows.map(r=>r.rank),Array.from({length:20},(_,i)=>i+1));
 }
 assert.equal(g.rankMax,20);assert.equal(g.y(20),1510);
 assert.notDeepEqual(data.snapshots[0].rows.map(r=>r.name),data.snapshots.at(-1).rows.map(r=>r.name));
 for(let frame=0;frame<1320;frame++){
  const state=geometry(data,frame/60),visible=state.endpoints.filter(e=>e.alpha>1e-8);
  assert.ok(visible.length<=20);
  assert.ok(visible.every(e=>Number.isFinite(e.y)&&e.y>=state.plot.top&&e.y<=state.plot.bottom+40));
 }
});
test('missing boundary coverage and unreviewed selected identities stop the monthly edition',()=>{
 for(const edit of [
  raw=>{raw.result=raw.result.filter(s=>s.date.slice(0,10)>='2026-09-01');},
  raw=>{raw.result=raw.result.filter(s=>s.date.slice(0,10)<='2026-09-21');},
  raw=>{const s=raw.result.find(s=>s.date.startsWith('2026-09-07'));Object.values(s.extradata).find(r=>r.place===1).opponent.template='unknown world leader';},
 ]){const raw=fixture();edit(raw);assert.throws(()=>prepareMonthlyWorld(raw,options));}
});

test('October world top-20 includes reviewed 1w Team and 100 Thieves with their original ranks',()=>{
 const raw=JSON.parse(gunzipSync(fs.readFileSync(new URL('./fixtures/liquipedia-october-2026.json.gz',import.meta.url))));
 const data=prepareMonthlyWorld(raw,{month:'2026-10',collectedOn:'2026-11-01'});
 assert.equal(data.headline,'CS2 · МИРОВОЙ РЕЙТИНГ');
 assert.deepEqual(data.hookTitle,['КАК ИЗМЕНИЛСЯ','РЕЙТИНГ CS2?']);
 assert.equal(data.hookGame,'COUNTER-STRIKE 2');
 assert.equal(data.hookPeriod,'ОКТЯБРЬ 2026 · МИРОВОЙ ТОП-20');
 assert.equal(data.titlePeriod,'ОКТЯБРЯ 2026');
 assert.equal(data.snapshots.length,5);assert.equal(data.names.length,26);
 assert.equal(data.snapshots[0].date,'2026-09-28');assert.equal(data.snapshots.at(-1).date,'2026-10-26');
 for(const [name,rank,points] of [['1w Team',18,1527.5],['100 Thieves',20,1498.1]]){
  const row=data.snapshots.at(-1).rows.find(r=>r.name===name);
  assert.equal(row.globalRank,rank);assert.equal(row.rank,rank);assert.equal(row.points,points);
  assert.match(data.palette[name],/^#[a-f0-9]{6}$/i);
  assert.match(data.logos[row.logoName],/^data:image\/png;base64,/);
 }
 assert.equal(monthlyCaptionFor(data),'VRS — итоги октября 2026\nКак менялся мировой топ-20 за месяц\n\nИсточник: LiquipediaDB.');
});

test('monthly logos stay on their traces and advance on a fixed time axis without dodging or rewind',()=>{
 const raw=JSON.parse(gunzipSync(fs.readFileSync(new URL('./fixtures/liquipedia-october-2026.json.gz',import.meta.url))));
 const data=prepareMonthlyWorld(raw,{month:'2026-10',collectedOn:'2026-11-01'});
 let previous;
 for(let frame=0;frame<1320;frame++){
  const g=geometry(data,frame/60);
  assert.equal(g.left,0);assert.equal(g.span,28);
  for(const endpoint of g.endpoints){
   assert.equal(g.offsets.get(endpoint.id),endpoint.x);
   const old=previous?.endpoints.find(e=>e.id===endpoint.id);
   if(old)assert.ok(endpoint.x>=old.x-1e-6,`${endpoint.name} rewinds at ${frame}`);
  }
  previous=g;
 }
 assert.equal(data.stableTable,true);assert.equal(data.graphMarkers,'logos');
});

test('October entries and exits are real top-20 changes and returning teams do not bridge absences',()=>{
 const raw=JSON.parse(gunzipSync(fs.readFileSync(new URL('./fixtures/liquipedia-october-2026.json.gz',import.meta.url))));
 const data=prepareMonthlyWorld(raw,{month:'2026-10',collectedOn:'2026-11-01'});
 assert.ok(data.snapshots[0].rows.some(r=>r.name==='Liquid'));
 assert.ok(!data.snapshots.at(-1).rows.some(r=>r.name==='Liquid'));
 assert.ok(!data.snapshots[0].rows.some(r=>r.name==='100 Thieves'));
 assert.ok(data.snapshots.at(-1).rows.some(r=>r.name==='100 Thieves'));
 const entering=geometry(data,1.8+data.stepSeconds*3.75);
 assert.ok(entering.endpoints.some(e=>e.name==='100 Thieves'&&e.alpha>0));
 assert.equal(trace(entering,'100 Thieves'),'');
 assert.ok(boundaryTrace(entering,'100 Thieves'));
 assert.ok(boundaryTrace(geometry(data,22),'100 Thieves'));
 const start=geometry(data,1.8+data.stepSeconds*3.501),end=geometry(data,1.8+data.stepSeconds*3.999);
 const head=g=>g.endpoints.find(e=>e.name==='100 Thieves');
 assert.ok(head(start).y>start.plot.bottom);
 assert.ok(head(end).y<head(start).y);
 assert.ok(Math.abs(head(end).y-end.y(20))<.01);
 for(const snapshot of data.snapshots)for(const row of snapshot.rows){
  assert.ok(data.palette[row.name]);assert.ok(data.logos[row.logoName]);
 }
});

test('entry and exit trails remain through every later frame after logos have left the top 20',()=>{
 const raw=JSON.parse(gunzipSync(fs.readFileSync(new URL('./fixtures/liquipedia-october-2026.json.gz',import.meta.url))));
 const data=prepareMonthlyWorld(raw,{month:'2026-10',collectedOn:'2026-11-01'}),previous=new Map();
 for(let frame=0;frame<1320;frame++){
  const g=geometry(data,frame/60);
  for(const name of data.names){
   const count=(boundaryTrace(g,name).match(/M/g)||[]).length;
   assert.ok(count>=(previous.get(name)||0),`${name} loses its trail at frame ${frame}`);
   previous.set(name,count);
  }
 }
 const final=geometry(data,22);
 for(const name of data.names){
  const changes=data.snapshots.slice(1).filter((s,i)=>Boolean(s.rows.find(r=>r.name===name))!==Boolean(data.snapshots[i].rows.find(r=>r.name===name))).length;
  assert.equal((boundaryTrace(final,name).match(/M/g)||[]).length,changes,name);
 }
 assert.ok(boundaryTrace(final,'Liquid'));
 assert.ok(!final.endpoints.some(e=>e.name==='Liquid'&&e.alpha>0));
});
