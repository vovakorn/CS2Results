import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {gunzipSync} from 'node:zlib';
import {S3Client,PutObjectCommand,GetObjectCommand,HeadObjectCommand} from '@aws-sdk/client-s3';
import {prepareRegions,fetchHistory,captionFor,CAMPAIGN,START,MAX_SOURCE_BYTES,isoDate} from '../data.mjs';
import {renderVideo,sha256} from './render-core.mjs';

const here=path.dirname(fileURLToPath(import.meta.url));
export const PREFIX=`vrs-major/${CAMPAIGN}/`;
const summary=data=>({region:data.regionSlug,teams:data.names.length,snapshots:data.snapshots.length,
 firstDate:data.snapshots[0].date,lastDate:data.snapshots.at(-1).date,sourceSha256:data.inputSha256});

export function s3Store(env,clientOverride){
 if(!env.OBJECT_STORAGE_BUCKET||!env.AWS_ACCESS_KEY_ID||!env.AWS_SECRET_ACCESS_KEY)throw Error('Storage is not configured');
 const client=clientOverride||new S3Client({region:'ru-central1',endpoint:'https://storage.yandexcloud.net',forcePathStyle:true,
  requestChecksumCalculation:'WHEN_REQUIRED',responseChecksumValidation:'WHEN_REQUIRED',
  credentials:{accessKeyId:env.AWS_ACCESS_KEY_ID,secretAccessKey:env.AWS_SECRET_ACCESS_KEY},maxAttempts:2});
 const Bucket=env.OBJECT_STORAGE_BUCKET;
 return {
  async get(Key){
   if(!Key.startsWith(`${PREFIX}sources/`))throw Error('Invalid storage scope');
   const result=await client.send(new GetObjectCommand({Bucket,Key}));
   const chunks=[];let length=0;
   for await(const chunk of result.Body){length+=chunk.length;if(length>MAX_SOURCE_BYTES)throw Error('Stored source too large');chunks.push(chunk);}
   return Buffer.concat(chunks);
  },
  async put(Key,Body,ContentType){
   if(!Key.startsWith(PREFIX))throw Error('Invalid storage scope');
   const digest=sha256(Body);
   try{await client.send(new PutObjectCommand({Bucket,Key,Body,ContentType,IfNoneMatch:'*',Metadata:{sha256:digest}}));}
   catch(error){
    if(error.$metadata?.httpStatusCode!==412)throw error;
    const old=await client.send(new HeadObjectCommand({Bucket,Key}));
    if(old.Metadata?.sha256!==digest)throw Error('Immutable object conflict');
   }
  },
 };
}

export function createHandler({env=process.env,now=()=>new Date(),fetcher=fetchHistory,renderer=renderVideo,storeFactory=s3Store,
 assetsDir=path.join(here,'assets'),fixture=path.join(here,'fixtures/liquipedia-history.json.gz')}={}){
 return async function handle(input){
  let stage='request';let directory;
  try{
   const event=typeof input?.body==='string'?JSON.parse(input.body):input;
   if(!event||!['capture','render'].includes(event.job)||
      (event.dry_run!==undefined&&typeof event.dry_run!=='boolean')||
      (event.render_probe!==undefined&&typeof event.render_probe!=='boolean'))throw Error('Invalid request');
   const dry=event.dry_run!==false,probe=event.render_probe===true;
   const today=now().toISOString().slice(0,10),through=event.through||today;
   if(!isoDate(through)||through<START||through>today)throw Error('Invalid requested period');
   if(event.job==='render'&&!['europe','americas','asia'].includes(event.region))throw Error('A valid region is required');
   if(probe&&(!dry||event.job!=='render'||event.source_key))throw Error('Probe must be a render dry run');
   if(event.source_key&&!new RegExp(`^${PREFIX}sources/[a-f0-9]{64}\\.json$`).test(event.source_key))throw Error('Invalid source key');
   stage='source';let raw,sourceBytes,store;
   if(probe){raw=JSON.parse(gunzipSync(await fs.readFile(fixture)));sourceBytes=Buffer.from(JSON.stringify(raw));}
   else if(event.source_key){store=storeFactory(env);sourceBytes=await store.get(event.source_key);raw=JSON.parse(sourceBytes);}
   else{raw=await fetcher({apiKey:env.LIQUIPEDIA_API_KEY,through});sourceBytes=Buffer.from(JSON.stringify(raw));}
   if(sourceBytes.length>MAX_SOURCE_BYTES)throw Error('Source exceeds limit');
   const inputSha256=sha256(sourceBytes);
   if(event.source_key&&!event.source_key.endsWith(`${inputSha256}.json`))throw Error('Source checksum mismatch');
   stage='prepare';const regions=prepareRegions(raw,{collectedOn:today,through,inputSha256});
   const selected=regions.find(r=>r.regionSlug===event.region);
   const result={job:event.job,dry_run:dry,campaign:CAMPAIGN,through,regions:regions.map(summary)};
   if(dry&&!probe)return {statusCode:200,body:JSON.stringify(result)};
   const sourceKey=`${PREFIX}sources/${inputSha256}.json`;
   if(!dry){store??=storeFactory(env);stage='persist_source';await store.put(sourceKey,sourceBytes,'application/json');result.sourceKey=sourceKey;}
   if(event.job==='render'){
    stage='render';directory=await fs.mkdtemp(path.join(os.tmpdir(),'vrs-major-'));
    const started=Date.now();
    const media=await renderer(selected,directory,{ffmpeg:env.VRS_FFMPEG_PATH||path.join(here,'bin/ffmpeg'),assetsDir});
    result.render={...media,file:undefined,preview:undefined,seconds:Math.round((Date.now()-started)/10)/100};
    if(!dry){
     stage='persist_render';const prefix=`${PREFIX}renders/${event.region}/${inputSha256}/${media.sha256}`;
     await store.put(`${prefix}/reel.mp4`,await fs.readFile(media.file),'video/mp4');
     const preview=await fs.readFile(media.preview),data=JSON.stringify(selected);
     const previewKey=`${prefix}/preview-${sha256(preview)}.png`,dataKey=`${prefix}/data-${sha256(data)}.json`;
     await store.put(previewKey,preview,'image/png');await store.put(dataKey,data,'application/json');
     const caption=captionFor(selected),captionKey=`${prefix}/caption-${sha256(caption)}.txt`;
     await store.put(captionKey,caption,'text/plain; charset=utf-8');
     result.artifacts={videoKey:`${prefix}/reel.mp4`,previewKey,dataKey,captionKey};
     const manifest=JSON.stringify({...result,sourceKey}),manifestKey=`${prefix}/manifest-${sha256(manifest)}.json`;
     await store.put(manifestKey,manifest,'application/json');result.artifacts.manifestKey=manifestKey;
    }
   }
   return {statusCode:200,body:JSON.stringify(result)};
  }catch(error){
   console.error(JSON.stringify({event:'vrs_major_failed',stage,errorType:error?.name||'Error'}));
   return {statusCode:stage==='request'?400:502,body:JSON.stringify({error:'vrs_major_failed',stage})};
  }finally{if(directory)await fs.rm(directory,{recursive:true,force:true});}
 };
}
export const handler=createHandler();
