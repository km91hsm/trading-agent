import test from 'node:test';
import assert from 'node:assert/strict';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
test('worker persists failure cooldown/streak and restores it before site access after restart',async()=>{
 const state={enabled:false,ownerToken:'owner',ownedWindowId:7},calls=[],messages=event();
 const view={kind:'news',stream:'news_all',interval_s:30,url:'https://www.saveticker.com/news'};
 const tab={id:1,windowId:7,url:view.url+'#trader-saveticker=owner:news'};
 const realTimer=setTimeout,realNow=Date.now,base=realNow();let now=base,fail=true;
 Date.now=()=>now;globalThis.setTimeout=(fn,delay,...a)=>realTimer(fn,delay===2000?0:delay,...a);
 globalThis.chrome={storage:{local:{get:async()=>({...state}),set:async v=>Object.assign(state,v)}},runtime:{id:'ext',onMessage:messages,onStartup:event(),onInstalled:event(),connectNative:()=>{
 const p={onMessage:event(),onDisconnect:event(),disconnect(){},postMessage(m){calls.push(m);queueMicrotask(()=>p.onMessage.listeners.forEach(fn=>fn({ok:true,id:m.id,views:[view],view,job_id:'job',status:'partial',count:1})));}};return p;}},windows:{get:async()=>({id:7,state:'normal'}),update:async()=>{}},tabs:{query:async()=>[tab],update:async()=>{},sendMessage:async()=>({status:fail?'error':'partial',html:'',page_url:view.url,selection:{loaded:true}})},alarms:{onAlarm:event(),create:async()=>{},clear:async()=>{}},downloads:{onChanged:event(),onDeterminingFilename:event()}};
 const stop=index=>new Promise(r=>messages.listeners[index]({action:'stop'},{id:'ext'},r));
 const start=async(key)=>{const w=await import('../worker.js?persist-'+key);await new Promise(r=>realTimer(r,1));state.enabled=true;await w.tick();return w;};
 await start('first');assert.equal(state.collectionSchedule.entries[0].failure_streak,1);assert.equal(state.collectionSchedule.entries[0].retry_at,base+60000);await stop(0);
 now=base+31000;const second=await start('second');assert.equal(calls.filter(m=>m.type==='begin').length,1,'restart never shortens failed cooldown');
 now=base+61000;await second.tick();assert.equal(state.collectionSchedule.entries[0].failure_streak,2);assert.equal(state.collectionSchedule.entries[0].retry_at,base+181000);await stop(1);
 now=base+92000;const third=await start('third');assert.equal(calls.filter(m=>m.type==='begin').length,2,'normal due cannot bypass persisted future backoff');
 fail=false;now=base+182000;await third.tick();assert.equal(state.collectionSchedule.entries[0].failure_streak,undefined);
 fail=true;now=base+213000;await third.tick();assert.equal(state.collectionSchedule.entries[0].failure_streak,1);assert.equal(state.collectionSchedule.entries[0].retry_at,base+273000);
 await stop(2);Date.now=realNow;globalThis.setTimeout=realTimer;
});
