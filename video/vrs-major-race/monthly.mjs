import identities from './assets/identities.json' with {type:'json'};
import catalog from './assets/catalog.json' with {type:'json'};
import {rankingRecords,isoDate} from './data.mjs';
import {sameCore} from './timeline.mjs';

const MONTHS=['январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь','декабрь'];
const GENITIVE=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
export const MONTHLY_PREFIX='vrs-monthly/';
export function monthlyPeriod(month,today){
 if(typeof month!=='string'||!/^20\d{2}-(0[1-9]|1[0-2])$/.test(month)||!isoDate(today))throw Error('Invalid month');
 const start=`${month}-01`,next=new Date(Date.parse(start)+32*86400000).toISOString().slice(0,7)+'-01';
 if(next>today)throw Error('Month is not closed');
 return {month,start,end:new Date(Date.parse(next)-86400000).toISOString().slice(0,10),
  from:new Date(Date.parse(start)-8*86400000).toISOString().slice(0,10),
  label:`${MONTHS[Number(month.slice(5))-1]} ${month.slice(0,4)}`,
  captionLabel:`${GENITIVE[Number(month.slice(5))-1]} ${month.slice(0,4)} года`};
}
const roster=o=>Object.values(o.players||{}).map(p=>p.displayName).filter(v=>typeof v==='string'&&v.trim());
const canonical=template=>identities.aliases[identities.names[template]]||identities.names[template];
export function prepareMonthlyWorld(raw,{month,collectedOn,inputSha256}={}){
 const period=monthlyPeriod(month,collectedOn);
 const all=rankingRecords(raw,{from:'2000-01-01'});
 const before=all.filter(s=>s.date.slice(0,10)<period.start).at(-1);
 const inside=all.filter(s=>s.date.slice(0,10)>=period.start&&s.date.slice(0,10)<=period.end);
 if(!before||before.date.slice(0,10)<period.from||!inside.length||
    Date.parse(period.end)-Date.parse(inside.at(-1).date.slice(0,10))>7*86400000)throw Error('Monthly source coverage is incomplete');
 const records=[before,...inside];
 const sorted=s=>Object.values(s.extradata).sort((a,b)=>a.place-b.place);
 const tops=records.map(s=>sorted(s).slice(0,20));
 for(const top of tops){
  if(top.length!==20||top.some((r,i)=>r.place!==i+1||!canonical(r.opponent.template)))throw Error('Unreviewed world top 20');
  if(new Set(top.map(r=>canonical(r.opponent.template))).size!==20)throw Error('Ambiguous world identities');
 }
 const names=[...new Set(tops.flatMap(top=>top.map(r=>canonical(r.opponent.template))))];
 const duplicateChoices=[];
 const snapshots=records.map((s,index)=>({date:s.date.slice(0,10),rows:tops[index].map(row=>{
  const template=row.opponent.template,name=canonical(template),display=identities.names[template];
  return {name,displayName:display,logoName:display,rank:row.place,globalRank:row.place,
   points:row.points,roster:roster(row.opponent),template};
 })}));
 for(const [old,next] of Object.entries(identities.aliases)){
  const a=snapshots.flatMap(s=>s.rows.filter(r=>r.displayName===old));
  const b=snapshots.flatMap(s=>s.rows.filter(r=>r.displayName===next));
  if(a.length&&b.length&&!sameCore(a.at(-1),b[0]))throw Error('Unverified monthly branding bridge');
 }
 return {...catalog,regionCode:'GLOBAL',regionSlug:'world',regionLabel:'МИРОВОЙ РЕЙТИНГ',
  names,snapshots,duplicateChoices,rankMode:'absolute',period,rankLimit:20,dynamicTop20:true,compactHeader:true,plotTop:454,
  graphMarkers:'logos',fixedViewport:true,stableTable:true,scoreOnly:true,hideDateTicks:true,hideCurrentDate:true,plotRight:760,
  soundtrack:'valve-cs2',zoomSeconds:0,endHold:2.8,
  headline:`VRS · ИТОГИ ${GENITIVE[Number(month.slice(5))-1].toUpperCase()} ${month.slice(0,4)}`,
  titlePeriod:`${GENITIVE[Number(month.slice(5))-1].toUpperCase()} ${month.slice(0,4)}`,
  subtitle:'ДИНАМИКА МИРОВОГО РЕЙТИНГА',
  cohortLabel:'МИРОВОЙ РЕЙТИНГ · ТОП-20',sourceLabel:'LIQUIPEDIA VRS',
  selection:'World top 20 at each dated ranking of the closed month; entries and exits remain gaps',
  rankDerivation:'Original global rank, never ordinal position within the selected teams',
  inputSha256,collectedOn,sourceType:'vrs_ranking_live',stepSeconds:17.4/(snapshots.length-1)};
}
export function monthlyCaptionFor(data){
 const label=data.period.captionLabel.replace(/ года$/,'');
 return `VRS — итоги ${label}\nКак менялся мировой топ-20 за месяц\n\nИсточник: LiquipediaDB.`;
}
