import {geometry,trace,fullDate,shortDate,valueLabel} from './timeline.mjs';
import {SAFE,PLOT,TABLE,COVER_HOLD,COVER_FADE} from './layout.mjs';
const WHITE='#f2f5f8', MUTED='#92a3b1';
const rgba=(hex,alpha)=>`rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${parseInt(hex.slice(5,7),16)},${alpha})`;
export function createPainter(data,{makeCanvas,makePath,images,projectLogo}){
 const dense=data.names.length>20,headSize=dense?36:42;
 const sprites=new Map(),tables=new Map(),dates=new Map();
 const base=makeCanvas(1080,1920),b=base.getContext('2d');
 const initial=geometry(data,0);
 function text(c,x,y,size,content,color=WHITE,align='left',title=false,maxWidth){
  c.fillStyle=color;
  const setFont=()=>{c.font=title?`${size}px "VRS Russo"`:`700 ${size}px Arial`;};
  setFont();content=String(content);
  if(maxWidth){
   const floor=Math.ceil(size*.75);
   while(size>floor&&c.measureText(content).width>maxWidth){size--;setFont();}
   if(c.measureText(content).width>maxWidth){
    while(content&&c.measureText(content+'…').width>maxWidth)content=content.slice(0,-1);
    content+='…';
   }
  }
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
 const header=b.createLinearGradient(88,258,912,480);
 header.addColorStop(0,'#15374bbf');header.addColorStop(.45,'#111d2bef');header.addColorStop(1,'#3b2a19bd');
 round(b,88,258,824,222,22,header,'#9ca8af30');
 circle(b,150,329,54,'#05090e','#efba6666');
 b.save();b.beginPath();b.arc(150,329,53,0,Math.PI*2);b.clip();imageFit(b,projectLogo,96,275,108,108);b.restore();
 text(b,222,282,22,'CS2 RESULTS');text(b,894,284,18,'@CS2RESULTS',MUTED,'right');
 text(b,222,329,34,data.headline||'ДОРОГА К MAJOR',WHITE,'left',true,672);
 text(b,222,381,19,data.subtitle||'COLOGNE → SINGAPORE · 2026','#bdc7cd','left',false,660);
 text(b,222,409,18,data.cohortLabel||`${data.regionLabel} · ${data.names.length} КОМАНД`,'#c3d4dd','left',false,660);
 text(b,222,440,16,data.sourceLabel,MUTED,'left',false,410);
 line(b,104,479,896,479,'#c0c8d740');
 for(const [x,label,align] of [[88,'ПОЗ.','left'],[140,data.rankMode==='absolute'?'МИРОВОЕ МЕСТО':`СРЕДИ ${data.names.length} КОМАНД`,'left'],[704,'ОЧКИ VRS','left'],[896,data.rankMode==='absolute'?'#МИР':'#РЕГ','right']])text(b,x,496,16,label,MUTED,align);
 b.save();b.globalAlpha=.12;
 circle(b,438,998,220,null,'#a8b5c0',3);circle(b,438,998,160,null,'#a8b5c0',2);
 line(b,180,998,690,998,'#a8b5c0',2);line(b,438,738,438,1254,'#a8b5c0',2);b.restore();
 b.save();b.globalAlpha=.055;b.textAlign='left';b.font='86px "VRS Russo"';b.fillStyle='#abb9c7';b.fillText('VRS',295,1029);b.restore();
 const gridStep=Math.max(5,Math.ceil(initial.rankMax/50)*5);
 const grid=data.rankMode==='absolute'?[...new Set([1,...Array.from({length:Math.floor(initial.rankMax/gridStep)},(_,i)=>(i+1)*gridStep),initial.rankMax])]:data.names.map((_,i)=>i+1);
 for(const rank of grid){
  line(b,PLOT.left,initial.y(rank),650,initial.y(rank),'#39556a4d');
  text(b,118,initial.y(rank)-11,22,rank,'#b8c4cd','right',false,36);
 }
 function table(index){
  if(tables.has(index))return tables.get(index);
  const canvas=makeCanvas(TABLE.width,TABLE.height),c=canvas.getContext('2d');
  c.fillStyle='#080d13';c.fillRect(0,0,TABLE.width,TABLE.height);
  const g=geometry(data,0);
  const ordered=[...data.names].sort((a,b)=>g.ranks[index][a]-g.ranks[index][b]);
  ordered.forEach((name,slot)=>{
   const row=g.row(index,name),y=g.rowY(slot+1)-TABLE.top,display=row?.displayName||name;
   const title=({'Natus Vincere':'NAVI','BETBOOM':'BetBoom','Ninjas in Pyjamas':'NIP',
    'Chinggis Warriors':'Chinggis W.','THUNDER dOWNUNDER':'THUNDER'})[display]||display;
   badge(c,name,[{name:row?.logoName||name,alpha:1}],20,y,26);
   text(c,44,y-(dense?20:23),dense?20:23,title,data.palette[name],'left',false,188);
   text(c,44,y+2,dense?23:26,row?valueLabel(row.points):'—',row?WHITE:MUTED,'left',false,124);
   text(c,232,y+5,20,row?`#${row.rank}`:'—','#bdcbd5','right',false,56);
  });tables.set(index,canvas);return canvas;
 }
 function date(index){
  if(dates.has(index))return dates.get(index);
  const canvas=makeCanvas(208,50),c=canvas.getContext('2d');
  c.drawImage(base,690,426,208,50,0,0,208,50);
  text(c,204,9,30,fullDate(data.snapshots[index].date),WHITE,'right');dates.set(index,canvas);return canvas;
 }
 // The cover occupies the initial hold; ranking movement still starts at 1.8 s.
 const cover=makeCanvas(SAFE.width,SAFE.height),c=cover.getContext('2d');
 c.fillStyle='#060a10';c.fillRect(0,0,SAFE.width,SAFE.height);
 const glow=c.createRadialGradient(500,900,20,500,900,760);
 glow.addColorStop(0,'#1b3546');glow.addColorStop(1,'#060a10');c.fillStyle=glow;c.fillRect(0,0,SAFE.width,SAFE.height);
 round(c,112,460,776,920,36,null,'#efba6666');
 circle(c,500,596,72,'#05090e','#efba6666');
 c.save();c.beginPath();c.arc(500,596,71,0,Math.PI*2);c.clip();imageFit(c,projectLogo,428,524,144,144);c.restore();
 text(c,500,706,28,'CS2 RESULTS',MUTED,'center');
 text(c,500,810,72,data.headline?'VRS · ИТОГИ':'ДОРОГА К',WHITE,'center',true,744);
 text(c,500,916,88,data.headline?'МЕСЯЦА':'MAJOR','#efba66','center',true,744);
 text(c,500,1070,40,data.subtitle||data.regionLabel,WHITE,'center',true,744);
 text(c,500,1150,28,data.cohortLabel||`${data.regionLabel} · ${data.names.length} КОМАНД`,MUTED,'center',false,744);
 text(c,500,1280,26,'@CS2RESULTS',MUTED,'center');
 return function paint(ctx,seconds){
  const g=geometry(data,seconds);ctx.save();ctx.globalAlpha=1;ctx.drawImage(base,0,0);
  if(g.i>0&&g.labelMix<1){ctx.drawImage(date(g.i-1),690,426);ctx.globalAlpha=g.labelMix;}
  ctx.drawImage(date(g.i),690,426);ctx.globalAlpha=1;
  ctx.save();ctx.globalAlpha=Math.min(1,Math.max(0,(seconds-1.8)/.2),Math.max(0,(27-seconds)/.2));
  text(ctx,894,465,12,`ПЕРЕХОД К ${shortDate(data.snapshots[g.after].date)}`,MUTED,'right');ctx.restore();
  ctx.save();ctx.beginPath();ctx.rect(132,516,518,1028);ctx.clip();
  for(const index of g.ticks){ctx.globalAlpha=.27*g.tickOpacity(g.days[index]);line(ctx,g.x(g.days[index]),524,g.x(g.days[index]),1540,'#39556a');}
  ctx.globalAlpha=.18;line(ctx,g.x(g.day),524,g.x(g.day),1540,'#abc3d2');ctx.globalAlpha=1;
  for(const name of data.names){
   const d=trace(g,name);if(d){
    const path=makePath(d),gradient=ctx.createLinearGradient(PLOT.left,0,430,0);
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
  if(g.i>0&&g.labelMix<1){ctx.drawImage(table(g.i-1),TABLE.left,TABLE.top);ctx.globalAlpha=g.labelMix;}
  ctx.drawImage(table(g.i),TABLE.left,TABLE.top);ctx.globalAlpha=1;
  ctx.save();ctx.beginPath();ctx.rect(104,1544,548,26);ctx.clip();
  for(const index of g.ticks){ctx.globalAlpha=g.tickOpacity(g.days[index]);text(ctx,g.x(g.days[index]),1546,17,shortDate(data.snapshots[index].date),MUTED,'center');}
  ctx.restore();
  const coverAlpha=Math.max(0,Math.min(1,(COVER_HOLD+COVER_FADE-seconds)/COVER_FADE));
  if(coverAlpha>0){ctx.globalAlpha=coverAlpha;ctx.drawImage(cover,0,0);}
  ctx.restore();
 };
}
