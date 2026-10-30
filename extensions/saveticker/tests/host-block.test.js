import test from 'node:test';
import assert from 'node:assert/strict';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
test('one observed host challenge blocks every lane across restart/start until explicit resume',async()=>{
 const state={enabled:false,ownerToken:'owner',ownedWindowId:7},calls=[],messages=event();
 const kinds=['news','community','market','calendar'];
 const views=kinds.map(kind=>({kind,stream:kind==='calendar'?'calendar_2026-10':kind+'_all',interval_s:30,url:'https://www.saveticker.com/'+(kind==='market'?'watchlist':kind)}));
 const tabs=views.map((v,i)=>({id:i+1,windowId:7,url:v.url+'#trader-saveticker=owner:'+['news','community','market','background'][i]}));
 let auth=true,captures=0,queries=0;
 const realTimer=setTimeout;
 globalThis.setTimeout=(fn,delay,...args)=>realTimer(fn,delay===2000?0:delay,...args);
 globalThis.chrome={storage:{local:{get:async()=>({...state}),set:async v=>Object.assign(state,v)}},
 runtime:{id:'ext',onMessage:messages,onStartup:event(),onInstalled:event(),connectNative:()=>{
 const p={onMessage:event(),onDisconnect:event(),disconnect(){},postMessage(m){
 calls.push(m);const result={ok:true,id:m.id,views,view:views.find(v=>v.stream===m.stream),job_id:'job',status:'partial',count:1};
 if(m.type==='snapshot'&&auth)Object.assign(result,{ok:false,error:'snapshot_failed'});
 queueMicrotask(()=>p.onMessage.listeners.forEach(fn=>fn(result)));
 }};return p;}},windows:{get:async()=>({id:7,state:'normal'}),update:async()=>{}},
 tabs:{query:async()=>{queries++;return tabs;},update:async()=>{},sendMessage:async()=>{captures++;return{status:auth?'auth_required':'partial',page_url:views[0].url,html:'',selection:{loaded:!auth},reason:auth?'verification_required':'scroll_limit'};}},
 alarms:{onAlarm:event(),create:async()=>{},clear:async()=>{}},downloads:{onChanged:event(),onDeterminingFilename:event()}};
 const first=await import('../worker.js?hostblock-first');await new Promise(r=>realTimer(r,1));state.enabled=true;await first.tick();
 assert.equal(captures,1);assert.equal(calls.filter(m=>m.type==='begin').length,1,'no later lane begins after challenge');
 assert.deepEqual(new Set(state.blockedLanes),new Set(['news','community','market','background']));
 assert.equal(calls.filter(m=>m.type==='state'&&m.state==='waiting_verification').length,4);
 const action=(index,action)=>new Promise(r=>messages.listeners[index]({action},{id:'ext'},r));
 const beforeBlockedQueries=queries;await first.tick();assert.equal(queries,beforeBlockedQueries,'blocked future tick never queries tabs');
 await action(0,'stop');
 const restarted=await import('../worker.js?hostblock-restart');await new Promise(r=>realTimer(r,1));
 state.enabled=true;await restarted.tick();assert.equal(captures,1,'persisted barrier survives new worker');assert.equal(queries,beforeBlockedQueries,'restored barrier prevents tabs lookup/creation/navigation');
 await action(1,'start');await restarted.tick();assert.equal(captures,1,'start does not clear barrier');assert.equal(queries,beforeBlockedQueries);
 auth=false;await action(1,'resume');await restarted.tick();
 assert.deepEqual(state.blockedLanes,[]);assert.equal(captures,4,'explicit resume attempts untouched nonauth lanes while retaining prior cooldown');
 await action(1,'stop');globalThis.setTimeout=realTimer;
});
