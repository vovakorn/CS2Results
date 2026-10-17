import assert from 'node:assert/strict';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const {createHandler}=await import(pathToFileURL(path.resolve(process.argv[2]||'build/vrs-major-function/handler.mjs')));
const handler=createHandler({now:()=>new Date('2026-10-16T12:00:00Z')});
const response=await handler(process.argv[3]==='monthly'?{job:'monthly',month:'2026-09',dry_run:true,render_probe:true}:
 {job:'render',region:'americas',dry_run:true,render_probe:true});
assert.equal(response.statusCode,200,response.body);
const result=JSON.parse(response.body);
assert.equal(result.render.frames,1944);assert.equal(result.render.width,1080);assert.equal(result.render.height,1920);
assert.equal(result.render.fps,60);assert.equal(result.render.duration,32.4);assert.ok(result.render.audioPeakDb<0);
console.log(JSON.stringify(result));
