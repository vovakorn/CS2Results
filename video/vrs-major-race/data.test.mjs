import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {prepareRegions,fetchHistory} from './data.mjs';
import {duration,frameCount,geometry} from './timeline.mjs';
const read=name=>JSON.parse(gunzipSync(fs.readFileSync(new URL(`./fixtures/${name}.json.gz`,import.meta.url))));
const fixture=()=>read('liquipedia-history');
const options={through:'2026-10-16',collectedOn:'2026-10-16'};
test('production cohort exactly preserves the approved 646 observations, identities and duplicate choices',()=>{
 const expected=read('expected-regions'),actual=prepareRegions(fixture(),options);
 assert.deepEqual(actual.map(r=>r.names.length),[23,12,9]);
 for(let i=0;i<3;i++)for(const key of ['names','snapshots','duplicateChoices'])assert.deepEqual(actual[i][key],expected[i][key]);
});
test('partial source, duplicate dates, ambiguous ranks and unreviewed identities fail closed',()=>{
 const edits=[r=>r.result.splice(1),r=>{r.result[1].date=r.result[0].date;},r=>{Object.values(r.result[0].extradata)[0].place=2;},
  r=>{Object.values(r.result[0].extradata).find(x=>x.opponent.extradata.region==='EU').opponent.template='unreviewed template';}];
 for(const edit of edits){const raw=fixture();edit(raw);assert.throws(()=>prepareRegions(raw,options));}
});
test('an additional real observation keeps the 32.4-second score and correct final date',()=>{
 const raw=fixture(),extra=structuredClone([...raw.result].sort((a,b)=>a.date.localeCompare(b.date)).at(-1));extra.date='2026-10-12 00:00:00';raw.result.push(extra);
 for(const data of prepareRegions(raw,options)){
  assert.equal(frameCount(data),1944);assert.ok(Math.abs(duration(data)-32.4)<1e-9);
  const end=geometry(data,32.4-1/60);assert.equal(data.snapshots[end.i].date,'2026-10-12');assert.equal(end.labelMix,1);
 }
});
test('fetch preserves split UTF-8 and rejects upstream errors without exposing the key',async()=>{
 const bytes=Buffer.from(JSON.stringify({result:[{name:'Тест'}]})),split=bytes.findIndex(b=>b>=128)+1;
 const fetchImpl=async(url,options)=>{
  assert.equal(url.hostname,'api.liquipedia.net');assert.equal(options.redirect,'error');
  return {ok:true,body:(async function*(){yield bytes.subarray(0,split);yield bytes.subarray(split);})()};
 };
 assert.equal((await fetchHistory({apiKey:'secret',through:'2026-10-16',fetchImpl})).result[0].name,'Тест');
 await assert.rejects(fetchHistory({apiKey:'secret',through:'2026-10-16',fetchImpl:async()=>({ok:false})}),e=>!e.message.includes('secret'));
});
