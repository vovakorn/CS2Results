import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {gunzipSync} from 'node:zlib';
import {createHandler,s3Store,PREFIX} from './handler.mjs';
import {sha256} from './render-core.mjs';
const fixture=new URL('../fixtures/liquipedia-history.json.gz',import.meta.url);
const raw=()=>fs.readFile(fixture).then(b=>JSON.parse(gunzipSync(b)));
const options={env:{LIQUIPEDIA_API_KEY:'private-do-not-print'},now:()=>new Date('2026-10-16T10:00:00Z'),fetcher:raw,fixture};
test('capture defaults to a read-only dry run without storage or rendering',async()=>{
 const handle=createHandler({...options,storeFactory:()=>{throw Error('Unexpected mutation');},renderer:()=>{throw Error('Unexpected render');}});
 const response=await handle({job:'capture'});assert.equal(response.statusCode,200);
 const body=JSON.parse(response.body);assert.equal(body.dry_run,true);assert.deepEqual(body.regions.map(r=>r.teams),[23,12,9]);
 assert.ok(!response.body.includes('private-do-not-print'));
});
test('real capture only stores a content-addressed source within the campaign',async()=>{
 const writes=[];
 const handle=createHandler({...options,storeFactory:()=>({put:async(...args)=>writes.push(args)})});
 const response=await handle({job:'capture',dry_run:false});assert.equal(response.statusCode,200);
 assert.equal(writes.length,1);assert.equal(writes[0][0],`${PREFIX}sources/${sha256(writes[0][1])}.json`);
});
test('invalid scope, missing region, string dry-run and a mutating probe are rejected before side effects',async()=>{
 const handle=createHandler({...options,storeFactory:()=>{throw Error('Unexpected storage');}});
 for(const event of [{job:'render'},{job:'capture',dry_run:'false'},{job:'render',region:'americas',dry_run:false,render_probe:true},
  {job:'capture',source_key:'../claims/x.json'},{job:'capture',through:'2027-01-01'},{job:'publish'}]){
  assert.equal((await handle(event)).statusCode,400);
 }
});
test('render probe uses bundled observations, never calls API or storage, and cleans temporary media',async()=>{
 let temporary;
 const handle=createHandler({...options,fetcher:()=>{throw Error('Unexpected API');},storeFactory:()=>{throw Error('Unexpected storage');},renderer:async(data,directory)=>{
  temporary=directory;assert.equal(data.regionSlug,'americas');await fs.writeFile(`${directory}/marker`,'test');return {frames:1944};
 }});
 const response=await handle({job:'render',region:'americas',dry_run:true,render_probe:true});assert.equal(response.statusCode,200);
 await assert.rejects(fs.stat(temporary));
});
test('immutable objects use private conditional writes; a conflicting object fails closed',async()=>{
 const calls=[],env={OBJECT_STORAGE_BUCKET:'private',AWS_ACCESS_KEY_ID:'x',AWS_SECRET_ACCESS_KEY:'y'};
 const store=s3Store(env,{send:async command=>{calls.push(command);return {};}});
 await store.put(`${PREFIX}sources/${'a'.repeat(64)}.json`,'{}','application/json');
 assert.equal(calls[0].input.IfNoneMatch,'*');assert.equal(calls[0].input.ACL,undefined);
 const conflict=s3Store(env,{send:async command=>{if(command.constructor.name==='PutObjectCommand')throw {$metadata:{httpStatusCode:412}};return {Metadata:{sha256:'different'}};}});
 await assert.rejects(conflict.put(`${PREFIX}sources/${'b'.repeat(64)}.json`,'{}','application/json'));
});
