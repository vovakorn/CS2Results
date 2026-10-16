import {build} from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url)),output=path.resolve(process.argv[2]);
await build({entryPoints:[path.join(here,'handler.mjs')],outfile:path.join(output,'handler.mjs'),
 bundle:true,platform:'node',target:'node22',format:'esm',external:['@napi-rs/canvas'],
 banner:{js:"import {createRequire as __vrsRequire} from 'node:module'; const require=__vrsRequire(import.meta.url);"}});
const require=createRequire(import.meta.url),canvas=path.dirname(require.resolve('@napi-rs/canvas'));
const native=path.dirname(createRequire(path.join(canvas,'index.js')).resolve('@napi-rs/canvas-linux-x64-gnu'));
for(const [src,name] of [[canvas,'canvas'],[native,'canvas-linux-x64-gnu']]){
 fs.cpSync(src,path.join(output,'node_modules/@napi-rs',name),{recursive:true,filter:s=>path.basename(s)!=='node_modules'});
}
fs.writeFileSync(path.join(output,'index.js'),`exports.handler=async(event,context)=>(await import('./handler.mjs')).handler(event,context);\n`);
