import identities from './assets/identities.json' with {type:'json'};
import catalog from './assets/catalog.json' with {type:'json'};
import {sameCore} from './timeline.mjs';

export const CAMPAIGN='cologne-singapore-2026';
export const START='2026-06-22';
export const MAX_SOURCE_BYTES=12*1024*1024;
const fail=(message)=>{throw new Error(message);};
const roster=opponent=>Object.values(opponent.players||{}).map(p=>p.displayName).filter(v=>typeof v==='string'&&v.trim());
export function captionFor(data){
 const region={europe:'Европа',americas:'Америка',asia:'Азия'}[data.regionSlug];
 return `Дорога к Major: ${region}.\n\nКак менялись места и очки VRS после Cologne: ${data.snapshots[0].date} — ${data.snapshots.at(-1).date}.\n\n`+
  `Линии — порядок среди ${data.names.length} показанных историй команд. Справа — очки и расчётное место в региональном списке Liquipedia. `+
  `В выборке все, кто находился в пределах региональной квоты на одной из дат, и два ближайших преследователя на последней дате. `+
  `Это история рейтинга, не подтверждённые приглашения.\n\nИсточник: LiquipediaDB vrs_ranking_live. Получено ${data.collectedOn}.\nCS2 Results · @cs2results\n#CS2 #VRS #Major #SingaporeMajor`;
}
export const isoDate=value=>typeof value==='string'&&/^2026-\d{2}-\d{2}$/.test(value)&&new Date(`${value}T00:00:00Z`).toISOString().slice(0,10)===value;

export function prepareRegions(raw,{collectedOn,through,inputSha256}={}){
 const records=[...(raw.result||[])].sort((a,b)=>a.date.localeCompare(b.date));
 if(records.length<2||records.length>40)fail('Expected 2..40 complete dated snapshots');
 const seen=new Set();
 for(const snap of records){
  const date=snap.date?.slice(0,10);
  if(!isoDate(date)||date<START||(through&&date>through)||seen.has(date)||snap.type!=='vrs_ranking_live')fail('Invalid snapshot date/type');
  seen.add(date);
  if(typeof snap.extradata==='string')snap.extradata=JSON.parse(snap.extradata);
  const rows=Object.values(snap.extradata||{});
  if(rows.length<100||rows.length>1000)fail('Incomplete or excessive snapshot rows');
  if(new Set(rows.map(r=>r.place)).size!==rows.length)fail('Ambiguous global ranks');
  for(const row of rows)if(!Number.isInteger(row.place)||row.place<1||!Number.isFinite(row.points)||row.points<=0||!row.opponent?.template)fail('Invalid ranking row');
 }
 const results=[];
 for(const [code,[slug,title,quota]] of Object.entries(identities.regions)){
  const complete=records.map(s=>Object.values(s.extradata).filter(r=>r.opponent.extradata?.region===code).sort((a,b)=>a.place-b.place).map((r,i)=>[i+1,r]));
  if(complete.some(rows=>rows.length<quota))fail('Incomplete regional source');
  const selected=new Set(complete.flatMap(rows=>rows.slice(0,quota).map(([,r])=>r.opponent.template)));
  for(const [,row] of complete.at(-1).slice(quota,quota+2))selected.add(row.opponent.template);
  for(const template of selected)if(!identities.names[template])fail(`Unreviewed template identity: ${template}`);
  for(const [old,next] of Object.entries(identities.aliases)){
   const before=complete.flatMap(rows=>rows.filter(([,r])=>identities.names[r.opponent.template]===old).map(([,r])=>({roster:roster(r.opponent)})));
   const after=complete.flatMap(rows=>rows.filter(([,r])=>identities.names[r.opponent.template]===next).map(([,r])=>({roster:roster(r.opponent)})));
   if(before.length&&after.length&&!sameCore(before.at(-1),after[0]))fail('Unverified branding bridge');
  }
  const names=[...new Set([...selected].map(t=>identities.aliases[identities.names[t]]||identities.names[t]))].sort();
  if(names.length>24)fail('Regional history exceeds the reviewed readable layout');
  const duplicateChoices=[];
  const snapshots=records.map((snapshot,index)=>{
   const best=new Map();
   for(const [rank,source] of complete[index]){
    const template=source.opponent.template;if(!selected.has(template))continue;
    const display=identities.names[template],name=identities.aliases[display]||display;
    if(best.has(name)){duplicateChoices.push({date:snapshot.date.slice(0,10),team:name,selected_global_rank:best.get(name).globalRank,other_global_rank:source.place});continue;}
    best.set(name,{name,displayName:display,logoName:display,rank,globalRank:source.place,points:source.points,roster:roster(source.opponent),region:code,template});
   }
   return {date:snapshot.date.slice(0,10),rows:[...best.values()]};
  });
  const current=new Map(snapshots.at(-1).rows.map(r=>[r.name,r.rank]));
  names.sort((a,b)=>(current.get(a)??9999)-(current.get(b)??9999));
  results.push({regionCode:code,regionSlug:slug,regionLabel:title,quota,names,snapshots,
   ...catalog,duplicateChoices,brandBridges:identities.aliases,
   selection:'Union of regional top-quota at all saved dates, plus two nearest teams after the quota at the latest date',
   rankDerivation:'Sort all saved global rows of this source region by their original global rank; number before deduplication',
   eligibility:'Rank history only; acceptance, intact roster and regional eligibility are not asserted',
   sourceLabel:'LIQUIPEDIA VRS',inputSha256,collectedOn,sourceType:'vrs_ranking_live',stepSeconds:25.2/(records.length-1)});
 }
 return results;
}

export async function fetchHistory({apiKey,through,fetchImpl=fetch}){
 if(!apiKey)fail('Liquipedia credentials are unavailable');
 if(!isoDate(through)||through<START)fail('Invalid through date');
 const result=[];let bytes=0;
 for(let offset=0;offset<1000;offset+=100){
  const url=new URL('https://api.liquipedia.net/api/v3/datapoint');
  url.search=new URLSearchParams({wiki:'counterstrike',limit:'100',offset:String(offset),query:'date,type,extradata',
   conditions:`[[type::vrs_ranking_live]] AND [[date::>2026-06-21]] AND [[date::<${new Date(Date.parse(through)+86400000).toISOString().slice(0,10)}]]`,order:'date asc'});
  const response=await fetchImpl(url,{headers:{Authorization:`Apikey ${apiKey}`,Accept:'application/json','User-Agent':'CS2ResultsBot/1.0 (VRS history)'},redirect:'error',signal:AbortSignal.timeout(20000)});
  if(!response.ok)fail('Liquipedia request failed');
  const chunks=[];
  for await(const chunk of response.body){bytes+=chunk.length;if(bytes>MAX_SOURCE_BYTES)fail('Source exceeds response limit');chunks.push(Buffer.from(chunk));}
  const page=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!Array.isArray(page.result))fail('Invalid Liquipedia envelope');
  result.push(...page.result);
  if(page.result.length<100)return {result};
 }
 fail('Source pagination limit reached');
}
