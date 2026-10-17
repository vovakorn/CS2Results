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
 const closing=sorted(records.at(-1)).slice(0,20);
 if(closing.length!==20||closing.some((r,i)=>r.place!==i+1||!canonical(r.opponent.template)))throw Error('Unreviewed world top 20');
 const names=closing.map(r=>canonical(r.opponent.template));
 if(new Set(names).size!==20)throw Error('Ambiguous final world identities');
 const selected=new Set(names),duplicateChoices=[];
 const snapshots=records.map(s=>{
  const best=new Map();
  for(const row of sorted(s)){
   const template=row.opponent.template,name=canonical(template);
   if(!selected.has(name))continue;
   if(best.has(name)){duplicateChoices.push({date:s.date.slice(0,10),team:name,other_global_rank:row.place});continue;}
   const display=identities.names[template];
   best.set(name,{name,displayName:display,logoName:display,rank:row.place,globalRank:row.place,
    points:row.points,roster:roster(row.opponent),template});
  }
  return {date:s.date.slice(0,10),rows:[...best.values()]};
 });
 for(const [old,next] of Object.entries(identities.aliases)){
  const a=snapshots.flatMap(s=>s.rows.filter(r=>r.displayName===old));
  const b=snapshots.flatMap(s=>s.rows.filter(r=>r.displayName===next));
  if(a.length&&b.length&&!sameCore(a.at(-1),b[0]))throw Error('Unverified monthly branding bridge');
 }
 return {...catalog,regionCode:'GLOBAL',regionSlug:'world',regionLabel:'МИРОВОЙ РЕЙТИНГ',
  names,snapshots,duplicateChoices,rankMode:'absolute',period,
  headline:'VRS · ИТОГИ МЕСЯЦА',subtitle:period.label.toUpperCase(),
  cohortLabel:'МИРОВОЙ РЕЙТИНГ · ТОП-20',sourceLabel:'LIQUIPEDIA VRS',
  selection:'World top 20 at the last available ranking of the closed month',
  rankDerivation:'Original global rank, never ordinal position within the selected teams',
  inputSha256,collectedOn,sourceType:'vrs_ranking_live',stepSeconds:25.2/(snapshots.length-1)};
}
export function monthlyCaptionFor(data){
 return `Изменения VRS по итогам ${data.period.captionLabel}.\n\n`+
  `Мировой рейтинг: как менялись позиции 20 команд, вошедших в топ-20 на конец месяца. `+
  `На графике и справа — реальные мировые места.\n\n`+
  `Календарный месяц: ${data.period.start} — ${data.period.end}. `+
  `Начальный рейтинг: ${data.snapshots[0].date}; последний рейтинг месяца: ${data.snapshots.at(-1).date}. `+
  `Показаны доступные датированные рейтинги; пропущенные обновления не восстановлены.\n\n`+
  `Источник: LiquipediaDB vrs_ranking_live. Получено ${data.collectedOn}.\n`+
  `CS2 Results · @cs2results\n#CS2 #VRS #CS2Results`;
}
