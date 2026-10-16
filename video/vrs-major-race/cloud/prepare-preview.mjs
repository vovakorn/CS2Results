import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {gunzipSync} from 'node:zlib';
import {prepareRegions} from '../data.mjs';
const here=path.dirname(fileURLToPath(import.meta.url)),base=path.resolve(here,'..'),root=path.resolve(base,'../..');
const raw=process.argv[2]?JSON.parse(await fs.readFile(process.argv[2],'utf8')):
 JSON.parse(gunzipSync(await fs.readFile(path.join(base,'fixtures/liquipedia-history.json.gz'))));
const regions=prepareRegions(raw,{collectedOn:process.argv[2]?new Date().toISOString().slice(0,10):'2026-10-12'});
const output=path.join(base,'public');await fs.mkdir(output,{recursive:true});
await fs.writeFile(path.join(output,'regional-data.json'),JSON.stringify(regions));
for(const [source,name] of [[path.join(root,'cs2bot/assets/channel-logo.png'),'channel-logo.png'],
 [path.join(root,'cs2bot/assets/fonts/RussoOne-Regular.ttf'),'RussoOne-Regular.ttf'],
 [path.join(base,'assets/Arimo-Bold.ttf'),'Arimo-Bold.ttf'],[path.join(base,'assets/road-to-major.wav'),'original-soundtrack.wav']])await fs.copyFile(source,path.join(output,name));
console.log(JSON.stringify({regions:regions.map(r=>({region:r.regionSlug,teams:r.names.length,lastDate:r.snapshots.at(-1).date})),source:process.argv[2]||'Reviewed saved fixture'}));
