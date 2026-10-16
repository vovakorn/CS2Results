import React,{useEffect,useLayoutEffect,useRef,useState} from 'react';
import {AbsoluteFill,cancelRender,continueRender,delayRender,staticFile,useCurrentFrame,useVideoConfig} from 'remotion';
import {createPainter} from './paint-frame.mjs';

export const VRSReel=({data})=>{
 const frame=useCurrentFrame(),{fps}=useVideoConfig();
 const canvas=useRef(null),painter=useRef(null),continued=useRef(false);
 const [ready,setReady]=useState(false);
 const [handle]=useState(()=>delayRender('Decode fonts and all logo textures'));
 useEffect(()=>{
  let active=true;
  const decode=async src=>{const image=new Image();image.src=src;await image.decode();return image;};
  (async()=>{
   await document.fonts.load('52px "VRS Russo"');
   await document.fonts.load('700 31px Arial');
   const images=Object.fromEntries(await Promise.all(Object.entries(data.logos).map(async([name,src])=>[name,await decode(src)])));
   const projectLogo=await decode(staticFile('channel-logo.png'));
   if(!active)return;
   painter.current=createPainter(data,{images,projectLogo,makePath:d=>new Path2D(d),
    makeCanvas:(width,height)=>{const c=document.createElement('canvas');c.width=width;c.height=height;return c;}});
   setReady(true);
  })().catch(cancelRender);
  return()=>{active=false;};
 },[data]);
 useLayoutEffect(()=>{
  if(ready){painter.current(canvas.current.getContext('2d'),frame/fps);if(!continued.current){continued.current=true;continueRender(handle);}}
 },[ready,frame,fps,handle]);
 return <AbsoluteFill style={{background:'#060a10'}}>
  <style>{`@font-face{font-family:"VRS Russo";src:url("${staticFile('RussoOne-Regular.ttf')}") format("truetype");font-weight:400}
   @font-face{font-family:Arial;src:url("${staticFile('Arimo-Bold.ttf')}") format("truetype");font-weight:700}`}</style>
  <canvas ref={canvas} width={1080} height={1920} style={{width:1080,height:1920}}/>
 </AbsoluteFill>;
};
