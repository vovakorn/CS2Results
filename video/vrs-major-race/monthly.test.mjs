import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {monthlyPeriod,prepareMonthlyWorld,monthlyCaptionFor} from './monthly.mjs';
import {geometry,frameCount} from './timeline.mjs';
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
 assert.equal(data.names.length,20);assert.equal(data.regionSlug,'world');
 assert.deepEqual(data.snapshots.map(s=>s.date),['2026-08-31','2026-09-07','2026-09-14','2026-09-21','2026-09-28']);
 assert.deepEqual(data.snapshots.at(-1).rows.map(r=>r.rank),Array.from({length:20},(_,i)=>i+1));
 assert.equal(frameCount(data),1944);
 assert.match(monthlyCaptionFor(data),/^Изменения VRS по итогам сентября 2026 года\./);
 assert.ok(!monthlyCaptionFor(data).includes('#Major'));
});
test('world animation retains actual ranks including positions below 20 throughout every frame',()=>{
 const data=prepareMonthlyWorld(fixture(),options),g=geometry(data,0);
 const below20=data.snapshots[0].rows.find(r=>r.rank>20);
 assert.ok(below20);assert.equal(g.ranks[0][below20.name],below20.rank);
 assert.equal(g.y(g.rankMax),1510);assert.equal(g.rowY(20),1510);
 for(let frame=0;frame<1944;frame++){
  const state=geometry(data,frame/60);
  assert.ok(state.endpoints.every(e=>Number.isFinite(e.y)&&state.offsets.has(e.id)));
 }
});
test('missing boundary coverage and unreviewed selected identities stop the monthly edition',()=>{
 for(const edit of [
  raw=>{raw.result=raw.result.filter(s=>s.date.slice(0,10)>='2026-09-01');},
  raw=>{raw.result=raw.result.filter(s=>s.date.slice(0,10)<='2026-09-21');},
  raw=>{const s=raw.result.find(s=>s.date.startsWith('2026-09-28'));Object.values(s.extradata).find(r=>r.place===1).opponent.template='unknown world leader';},
 ]){const raw=fixture();edit(raw);assert.throws(()=>prepareMonthlyWorld(raw,options));}
});
