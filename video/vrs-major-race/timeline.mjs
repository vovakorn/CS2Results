import {PLOT} from './layout.mjs';
export const FPS = 60;
export const HOLD = 1.8;
export const STEP = 1.8;
export const ZOOM = 1.2;
export const END = 4.2;
export const smooth = (v) => {const t = Math.min(1, Math.max(0, v)); return t * t * (3 - 2 * t);};
export const valueLabel = (v) => Number(v).toFixed(1).replace('.', ',');
export const shortDate = (date) => `${date.slice(8, 10)}.${date.slice(5, 7)}`;
export const fullDate = (date) => `${shortDate(date)}.${date.slice(0, 4)}`;
const key = (v) => v.trim().toLowerCase();

export const sameCore = (a, b) => {
  if (!a || !b) return false;
  const before = new Set(a.roster.map(key).filter(Boolean));
  const after = new Set(b.roster.map(key).filter(Boolean));
  return [...before].filter((p) => after.has(p)).length >= 3;
};
export const positions = (snapshot, names) => {
  const rows = new Map(snapshot.rows.map((r) => [key(r.name), r]));
  if (rows.size !== snapshot.rows.length) throw new Error('Ambiguous team name');
  const ordered = names.flatMap((name) => {
    const row = rows.get(key(name));
    return row ? [{name, row}] : [];
  }).sort((a, b) => a.row.rank - b.row.rank);
  for (const name of names) if (!rows.has(key(name))) ordered.push({name, row:null});
  return Object.fromEntries(ordered.map(({name}, i) => [name, i + 1]));
};
const stepSeconds = data => data.stepSeconds ?? STEP;
export const duration = (data) => HOLD + stepSeconds(data) * (data.snapshots.length - 1) + ZOOM + END;
export const frameCount = (data) => Math.round(duration(data) * FPS);
const prepared = new WeakMap();
const prepare = (data) => {
  if (prepared.has(data)) return prepared.get(data);
  const first = Date.parse(`${data.snapshots[0].date}T00:00:00Z`);
  const days = data.snapshots.map((s) => (Date.parse(`${s.date}T00:00:00Z`)-first)/86400000);
  const rankMax = data.rankMode==='absolute' ? Math.max(...data.snapshots.flatMap(s=>s.rows.map(r=>r.rank))) : data.names.length;
  const ranks = data.snapshots.map((s) => data.rankMode==='absolute' ?
    Object.fromEntries(data.names.map(name=>[name,s.rows.find(r=>key(r.name)===key(name))?.rank??rankMax+1])) : positions(s,data.names));
  const rows = data.snapshots.map((s) => new Map(s.rows.map((r)=>[key(r.name),r])));
  const row = (index,name) => rows[index]?.get(key(name));
  const cores = {};
  for (const name of data.names) {
    let segment = -1;
    cores[name] = data.snapshots.map((_,index) => {
      if (!row(index,name)) return null;
      if (!sameCore(row(index-1,name),row(index,name))) segment++;
      return `${name}:${segment}`;
    });
  }
  const result = {days,ranks,row,cores,rankMax};
  prepared.set(data,result);
  return result;
};

// One crossing plan per snapshot interval. Every lane starts/ends at zero,
// so changing the interval cannot teleport a badge into a new lane.
export const badgePositions = (endpoints, separation, progress) => {
  const placed = [];
  const offsets = new Map();
  for (const endpoint of endpoints) {
    let offset = 0;
    for (const previous of placed) {
      const before = endpoint.y0-previous.y0, after = endpoint.y1-previous.y1;
      if (before*after>0 || (before===0 && after===0)) continue;
      const crossing = Math.abs(before)/(Math.abs(before)+Math.abs(after));
      const side = progress<crossing ? (crossing-progress)/crossing :
        (progress-crossing)/Math.max(1e-9,1-crossing);
      const weight = 1-Math.min(1,Math.max(0,side))**3;
      const visible = smooth(Math.min(endpoint.alpha,previous.alpha)*4);
      offset = Math.min(offset,(previous.offset+previous.x-endpoint.x-separation*weight)*visible);
    }
    placed.push({...endpoint,offset,bx:endpoint.x+offset});
  }
  const left = Math.min(...placed.map((e)=>e.bx));
  const right = Math.max(...placed.map((e)=>e.bx));
  const shift = Math.max(0,PLOT.left-left);
  if (right+shift>PLOT.right+6.001) throw new Error('Badge group does not fit plot');
  for (const e of placed) offsets.set(e.id,e.bx+shift);
  return offsets;
};

export const geometry = (data, seconds) => {
  const {days,ranks,row,cores,rankMax} = prepare(data);
  const step=stepSeconds(data);
  const progress = Math.min(data.snapshots.length-1,Math.max(0,(seconds-HOLD)/step));
  const i = Math.min(data.snapshots.length-1,Math.floor(progress+1e-8));
  const fraction = Math.max(0,progress-i);
  const after = Math.min(i+1,data.snapshots.length-1);
  const eased = smooth(fraction);
  const day = days[i]+(days[after]-days[i])*fraction;
  const zoom = smooth((seconds-HOLD-step*(data.snapshots.length-1))/ZOOM);
  const left = Math.max(-7,day-35)*(1-zoom);
  const span = 42*(1-zoom)+days.at(-1)*zoom;
  const x = (d) => PLOT.left+(PLOT.right-PLOT.left)*(d-left)/span;
  const y = (position) => PLOT.top+(position-1)*(PLOT.bottom-PLOT.top)/Math.max(1,rankMax-1);
  const rowY = (position) => PLOT.top+(position-1)*(PLOT.bottom-PLOT.top)/Math.max(1,data.names.length-1);
  const animatedRanks = Object.fromEntries(data.names.map((name)=>[name,
    ranks[i][name]+(ranks[after][name]-ranks[i][name])*eased]));
  const endpoints = [];
  for (const name of data.names) {
    const beforeRow = row(i,name), afterRow = row(after,name);
    if (beforeRow && (i===after || sameCore(beforeRow,afterRow))) {
      const beforeLogo = beforeRow.logoName || name, afterLogo = afterRow.logoName || name;
      endpoints.push({id:cores[name][i],name,row:beforeRow,x:x(day),y:y(animatedRanks[name]),y0:y(ranks[i][name]),y1:y(ranks[after][name]),alpha:1,
        logoLayers:beforeLogo===afterLogo ? [{name:beforeLogo,alpha:1}] :
          [{name:beforeLogo,alpha:1-eased},{name:afterLogo,alpha:eased}]});
    } else {
      if (beforeRow) endpoints.push({id:cores[name][i],name,row:beforeRow,x:x(days[i]),
        y:y(ranks[i][name]),y0:y(ranks[i][name]),y1:y(ranks[i][name]),alpha:1-eased,logoLayers:[{name:beforeRow.logoName||name,alpha:1}]});
      if (afterRow) endpoints.push({id:cores[name][after],name,row:afterRow,x:x(days[after]),
        y:y(ranks[after][name]),y0:y(ranks[after][name]),y1:y(ranks[after][name]),alpha:eased,logoLayers:[{name:afterRow.logoName||name,alpha:1}]});
    }
  }
  const offsets = badgePositions(endpoints,data.names.length>20?47:54,eased);
  const labelMix = i===0?1:smooth((seconds-HOLD-step*i)/.22);
  const ticks = days.map((_,index)=>index).filter((index)=>index%3===0 || index===days.length-1);
  const tickOpacity = (d) => smooth((x(d)-116)/24)*smooth((652-x(d))/24);
  return {i,fraction,after,days,day,left,span,x,y,rowY,rankMax,ranks,animatedRanks,row,endpoints,offsets,ticks,tickOpacity,labelMix};
};

// Exact cubic subcurves retain the same shape as the visible end advances.
// Old segments stay in the SVG; the stationary viewport clips them continuously.
export const trace = (g, name) => {
  let path = '';
  for (let k=0;k<=g.i;k++) {
    const next=k+1;
    if (next>=g.days.length || !sameCore(g.row(k,name),g.row(next,name))) continue;
    const limit=k===g.i?g.fraction:1;
    if (limit<=0) continue;
    const x0=g.x(g.days[k]),dx=g.x(g.days[next])-x0;
    const y0=g.y(g.ranks[k][name]),dy=g.y(g.ranks[next][name])-y0;
    const value=(n)=>n.toFixed(3);
    path+=`M${value(x0)} ${value(y0)} C${value(x0+dx*limit/3)} ${value(y0)} `+
      `${value(x0+2*dx*limit/3)} ${value(y0+dy*limit*limit)} `+
      `${value(x0+dx*limit)} ${value(y0+dy*smooth(limit))} `;
  }
  return path;
};
