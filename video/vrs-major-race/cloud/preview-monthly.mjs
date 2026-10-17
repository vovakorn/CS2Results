// Offline preview from the reviewed dated fixture; never invokes a publisher.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {gunzipSync} from 'node:zlib';
import {prepareMonthlyWorld,monthlyCaptionFor} from '../monthly.mjs';
import {renderVideo,sha256} from './render-core.mjs';
const source=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const month=process.argv[2],output=path.resolve(process.argv[3]||`build/vrs-monthly-${month}`);
if(!month||!process.env.VRS_FFMPEG_PATH)throw Error('Pass YYYY-MM, output folder and VRS_FFMPEG_PATH');
const bytes=Buffer.from(JSON.stringify(JSON.parse(gunzipSync(await fs.readFile(path.join(source,'fixtures/liquipedia-history.json.gz'))))));
const data=prepareMonthlyWorld(JSON.parse(bytes),{month,collectedOn:new Date().toISOString().slice(0,10),inputSha256:sha256(bytes)});
await fs.mkdir(output,{recursive:true});
await fs.writeFile(path.join(output,'data.json'),JSON.stringify(data));
await fs.writeFile(path.join(output,'caption.txt'),monthlyCaptionFor(data));
const render=await renderVideo(data,output,{ffmpeg:process.env.VRS_FFMPEG_PATH,assetsDir:path.join(source,'assets')});
await fs.writeFile(path.join(output,'manifest.json'),JSON.stringify({...render,month,source:'reviewed offline fixture'},null,2));
console.log(JSON.stringify({output,month,...render}));
