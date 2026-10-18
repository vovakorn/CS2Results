import {geometry,trace,fullDate,shortDate,valueLabel} from './timeline.mjs';
const WHITE='#f2f5f8', MUTED='#92a3b1';
const rgba=(hex,alpha)=>`rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${parseInt(hex.slice(5,7),16)},${alpha})`;
export function createPainter(data,{makeCanvas,makePath,images,projectLogo}){
 const dense=data.names.length>20,headSize=dense?42:49;
 const sprites=new Map(),tables=new Map(),dates=new Map();
 const base=makeCanvas(1080,1920),b=base.getContext('2d');
 const initial=geometry(data,0);
 function text(c,x,y,size,content,color=WHITE,align='left',title=false){
  c.fillStyle=color;c.font=title?`${size}px "VRS Russo"`:`700 ${size}px Arial`;
  c.textAlign=align;c.textBaseline='alphabetic';
  const ascent=c.measureText(String(content)).actualBoundingBoxAscent;
  c.fillText(String(content),x,y+ascent);
 }
 function round(c,x,y,w,h,r,fill,stroke){
  c.beginPath();c.roundRect(x,y,w,h,r);
  if(fill){c.fillStyle=fill;c.fill();}if(stroke){c.strokeStyle=stroke;c.lineWidth=1;c.stroke();}
 }
 function line(c,x1,y1,x2,y2,color,width=1){
  c.beginPath();c.moveTo(x1,y1);c.lineTo(x2,y2);c.strokeStyle=color;c.lineWidth=width;c.stroke();
 }
 function circle(c,x,y,r,fill,stroke,width=1){
  c.beginPath();c.arc(x,y,r,0,Math.PI*2);if(fill){c.fillStyle=fill;c.fill();}
  if(stroke){c.strokeStyle=stroke;c.lineWidth=width;c.stroke();}
 }
 function imageFit(c,image,x,y,w,h){
  const scale=Math.min(w/image.width,h/image.height);
  const dw=image.width*scale,dh=image.height*scale;
  c.drawImage(image,x+(w-dw)/2,y+(h-dh)/2,dw,dh);
 }
 function sprite(name,logoName,size){
  const key=[name,logoName,size].join('|');if(sprites.has(key))return sprites.get(key);
  const canvas=makeCanvas(size,size),c=canvas.getContext('2d');
  const plate=data.logoPlates.includes(logoName)?'#eaf0f5':'#111b25';
  circle(c,size/2,size/2,size/2-1,plate,data.palette[name],2);
  c.save();c.beginPath();c.arc(size/2,size/2,size/2-2,0,Math.PI*2);c.clip();
  if(images[logoName])imageFit(c,images[logoName],6,6,size-12,size-12);
  else{
   c.font=`700 ${size/2}px Arial`;c.fillStyle=data.palette[name];c.textAlign='center';c.textBaseline='middle';
   c.fillText(logoName.slice(0,2),size/2,size/2);
  }
  c.restore();sprites.set(key,canvas);return canvas;
 }
 function badge(c,name,layers,x,y,size,alpha=1){
  if(alpha<=0)return;
  c.save();c.globalAlpha=alpha;
  c.drawImage(sprite(name,layers[0].name,size),x-size/2,y-size/2);
  for(const layer of layers.slice(1)){
   c.globalAlpha=alpha*layer.alpha;c.drawImage(sprite(name,layer.name,size),x-size/2,y-size/2);
  }c.restore();
 }
 b.fillStyle='#060a10';b.fillRect(0,0,1080,1920);
 const radial=b.createRadialGradient(180,820,0,180,820,1000);
 radial.addColorStop(0,'#14253288');radial.addColorStop(1,'#14253200');b.fillStyle=radial;b.fillRect(0,0,1080,1920);
 const header=b.createLinearGradient(60,82,990,368);
 header.addColorStop(0,'#15374bbf');header.addColorStop(.45,'#111d2bef');header.addColorStop(1,'#3b2a19bd');
 round(b,60,82,930,286,22,header,'#9ca8af30');
 circle(b,151,176,66,'#05090e','#efba6666');
 b.save();b.beginPath();b.arc(151,176,65,0,Math.PI*2);b.clip();imageFit(b,projectLogo,85,110,132,132);b.restore();
 text(b,246,119,25,'CS2 RESULTS');text(b,960,122,21,'@CS2RESULTS',MUTED,'right');
 text(b,244,169,data.headline?42:52,data.headline||'ДОРОГА К MAJOR',WHITE,'left',true);
 text(b,247,236,20,data.subtitle||'COLOGNE → SINGAPORE · 2026','#bdc7cd');
 text(b,247,279,20,data.cohortLabel||`${data.regionLabel} · ${data.names.length} КОМАНД`,'#c3d4dd');
 text(b,247,322,17,data.sourceLabel,MUTED);
 line(b,84,367,966,367,'#c0c8d740');
 for(const [x,label,align] of [[56,'ПОЗ.','left'],[111,data.rankMode==='absolute'?'МИРОВОЕ МЕСТО':`СРЕДИ ${data.names.length} КОМАНД`,'left'],[720,'ОЧКИ VRS','left'],[960,data.rankMode==='absolute'?'#МИР':'#РЕГ','right']])text(b,x,405,18,label,MUTED,align);
 b.save();b.globalAlpha=.12;
 circle(b,438,998,220,null,'#a8b5c0',3);circle(b,438,998,160,null,'#a8b5c0',2);
 line(b,180,998,690,998,'#a8b5c0',2);line(b,438,738,438,1254,'#a8b5c0',2);b.restore();
 b.save();b.globalAlpha=.055;b.textAlign='left';b.font='86px "VRS Russo"';b.fillStyle='#abb9c7';b.fillText('VRS',295,1029);b.restore();
 const gridStep=Math.max(5,Math.ceil(initial.rankMax/50)*5);
 const grid=data.rankMode==='absolute'?[...new Set([1,...Array.from({length:Math.floor(initial.rankMax/gridStep)},(_,i)=>(i+1)*gridStep),initial.rankMax])]:data.names.map((_,i)=>i+1);
 for(const rank of grid){
  line(b,108,initial.y(rank),672,initial.y(rank),'#39556a4d');
  text(b,91,initial.y(rank)-14,28,rank,'#b8c4cd','right');
 }
 function table(index){
  if(tables.has(index))return tables.get(index);
  const canvas=makeCanvas(289,1197),c=canvas.getContext('2d');
  c.fillStyle='#080d13';c.fillRect(0,0,289,1197);
  const g=geometry(data,0);
  const ordered=[...data.names].sort((a,b)=>g.ranks[index][a]-g.ranks[index][b]);
  ordered.forEach((name,slot)=>{
   const row=g.row(index,name),y=g.rowY(slot+1)-418,display=row?.displayName||name;
   const title=({'Natus Vincere':'NAVI','BETBOOM':'BetBoom','Ninjas in Pyjamas':'NIP',
    'Chinggis Warriors':'Chinggis W.','THUNDER dOWNUNDER':'THUNDER'})[display]||display;
   badge(c,name,[{name:row?.logoName||name,alpha:1}],25,y,30);
   text(c,54,y-(dense?23:26),dense?22:26,title,data.palette[name]);
   text(c,56,y+(dense?1:3),dense?26:31,row?valueLabel(row.points):'—',row?WHITE:MUTED);
   text(c,275,y-9,dense?22:25,row?`#${row.rank}`:'—','#bdcbd5','right');
  });tables.set(index,canvas);return canvas;
 }
 function date(index){
  if(dates.has(index))return dates.get(index);
  const canvas=makeCanvas(240,60),c=canvas.getContext('2d');
  c.drawImage(base,740,300,240,60,0,0,240,60);
  text(c,220,12,40,fullDate(data.snapshots[index].date),WHITE,'right');dates.set(index,canvas);return canvas;
 }
 return function paint(ctx,seconds){
  const g=geometry(data,seconds);ctx.save();ctx.globalAlpha=1;ctx.drawImage(base,0,0);
  if(g.i>0&&g.labelMix<1){ctx.drawImage(date(g.i-1),740,300);ctx.globalAlpha=g.labelMix;}
  ctx.drawImage(date(g.i),740,300);ctx.globalAlpha=1;
  ctx.save();ctx.globalAlpha=Math.min(1,Math.max(0,(seconds-1.8)/.2),Math.max(0,(27-seconds)/.2));
  text(ctx,960,373,19,`ПЕРЕХОД К ${shortDate(data.snapshots[g.after].date)}`,MUTED,'right');ctx.restore();
  ctx.save();ctx.beginPath();ctx.rect(108,418,566,1190);ctx.clip();
  for(const index of g.ticks){ctx.globalAlpha=.27*g.tickOpacity(g.days[index]);line(ctx,g.x(g.days[index]),427,g.x(g.days[index]),1608,'#39556a');}
  ctx.globalAlpha=.18;line(ctx,g.x(g.day),427,g.x(g.day),1608,'#abc3d2');ctx.globalAlpha=1;
  for(const name of data.names){
   const d=trace(g,name);if(d){
    const path=makePath(d),gradient=ctx.createLinearGradient(112,0,430,0);
    gradient.addColorStop(0,rgba(data.palette[name],.1));gradient.addColorStop(1,data.palette[name]);
    ctx.strokeStyle=gradient;ctx.lineJoin='round';ctx.lineCap='round';
    for(const [width,alpha] of [[12,.065],[8,.18],[4.5,1]]){ctx.lineWidth=width;ctx.globalAlpha=alpha;ctx.stroke(path);}
   }
   ctx.globalAlpha=.7;
   for(let k=0;k<=g.i;k++)if(g.row(k,name))circle(ctx,g.x(g.days[k]),g.y(g.ranks[k][name]),2.5,data.palette[name]);
  }ctx.restore();
  for(const head of g.endpoints){
   ctx.save();ctx.globalAlpha=head.alpha*Math.min(1,Math.abs(g.offsets.get(head.id)-head.x)/8);
   line(ctx,head.x,head.y,g.offsets.get(head.id),head.y,data.palette[head.name]);circle(ctx,head.x,head.y,3,data.palette[head.name]);ctx.restore();
   badge(ctx,head.name,head.logoLayers,g.offsets.get(head.id),head.y,headSize,head.alpha);
  }
  if(g.i>0&&g.labelMix<1){ctx.drawImage(table(g.i-1),685,418);ctx.globalAlpha=g.labelMix;}
  ctx.drawImage(table(g.i),685,418);ctx.globalAlpha=1;
  ctx.save();ctx.beginPath();ctx.rect(84,1620,600,70);ctx.clip();
  for(const index of g.ticks){ctx.globalAlpha=g.tickOpacity(g.days[index]);text(ctx,g.x(g.days[index]),1627,21,shortDate(data.snapshots[index].date),MUTED,'center');}
  ctx.restore();
  const intro=Math.min(1,.35+seconds/.65);
  if(intro<1){ctx.globalAlpha=1-intro;ctx.fillStyle='#060a10';ctx.fillRect(0,0,1080,1920);}
  ctx.restore();
 };
}
