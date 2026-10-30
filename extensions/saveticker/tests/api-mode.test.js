import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { apiCycle, retryDeadline } from '../api-runner.js';

const TOKEN = 'a'.repeat(32), URL_NEWS = 'https://www.saveticker.com/api/news/list?page=1&page_size=20&sort=created_at_desc&label_group=1&label_name=1';
const descriptor = { method:'GET',url:URL_NEWS,kind:'news',max_bytes:2097152 };
async function content(response, override={}) {
 const listeners=[], state={enabled:true,backend:'api',ownerToken:TOKEN}; let calls=0;
 const context={URL,AbortController,TextDecoder,Date,JSON,Set,Object,Number,Promise,RegExp,Error,setTimeout,clearTimeout,
 location:{origin:'https://www.saveticker.com',hash:'#trader-saveticker-api='+TOKEN},document:{title:'뉴스',querySelector:()=>null},
 chrome:{runtime:{id:'ext',onMessage:{addListener:f=>listeners.push(f)}},storage:{local:{get:async()=>state}}},
 fetch:async(...args)=>{calls++; assert.equal(args[1].credentials,'same-origin'); assert.equal(args[1].redirect,'manual'); return response;},...override};
 vm.runInNewContext(await readFile(new URL('../api-content.js',import.meta.url),'utf8'),context);
 return {state,send:(extra={},sender={id:'ext'})=>new Promise(resolve=>{const ret=listeners[0]({type:'api_fetch',token:TOKEN,job:{job_id:'b'.repeat(32),expires_at:new Date(Date.now()+60000).toISOString(),request:descriptor},...extra},sender,resolve);if(!ret)resolve(null);}),calls:()=>calls};
}
function jsonResponse(data, status=200, extra={}) {return new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json',...extra}});}
test('isolated API content uses browser auth and projects only collection data',async()=>{
 const r=jsonResponse({news_list:[{id:'1',title:'news',author_secret:'never',content:'body'}]});Object.defineProperty(r,'url',{value:URL_NEWS});
 const c=await content(r); const out=await c.send(); assert.equal(out.status,200);assert.equal(out.data.news_list[0].title,'news');assert.equal(out.data.news_list[0].author_secret,undefined);assert.equal(c.calls(),1);
});
test('foreign sender, unknown URL, stopped and wrong owner do not fetch',async()=>{
 const c=await content(jsonResponse({}));assert.equal(await c.send({}, {id:'other'}),null);
 assert.equal((await c.send({token:'wrong'})).error,'api_denied');
 assert.equal((await c.send({job:{job_id:'b'.repeat(32),expires_at:new Date(Date.now()+60000).toISOString(),request:{...descriptor,url:'https://evil.invalid/api/news/list'}}})).error,'api_contract_unverified');
 c.state.enabled=false; assert.equal((await c.send()).error,'api_stopped');assert.equal(c.calls(),0);
});
test('challenge, redirect mismatch, too large and rate limit are not normal data',async()=>{
 let c=await content(jsonResponse({}),{document:{title:'잠시만 기다려 주세요 · SAVE',querySelector:()=>null}});assert.equal((await c.send()).status,403);assert.equal(c.calls(),0);
 let r=jsonResponse({});Object.defineProperty(r,'url',{value:'https://www.saveticker.com/login'});c=await content(r);assert.equal((await c.send()).status,403);
 r=jsonResponse({padding:'a'.repeat(2097153)});Object.defineProperty(r,'url',{value:URL_NEWS});c=await content(r);assert.equal((await c.send()).error,'api_response_too_large');
 r=jsonResponse({},429,{'retry-after':'120'});Object.defineProperty(r,'url',{value:URL_NEWS});c=await content(r);assert.equal((await c.send()).status,429);assert.equal((await c.send()).retry_after,'120');
});
test('Retry-After respects server date/seconds and a conservative local floor',()=>{
 const now=Date.now();assert.equal(retryDeadline('120',now),now+120000);assert.equal(retryDeadline('invalid',now),now+60000);assert.ok(retryDeadline(new Date(now+300000).toUTCString(),now)>now+299000);
});
function runner(response={status:200,url:URL_NEWS,content_type:'application/json',data:{news_list:[]}}, opts={}) {
 const calls=[],state={},views=[{stream:'news_all',kind:'news',interval_s:30},{stream:'calendar_2026-10',kind:'calendar',interval_s:300}],done=[];let current=true,blocked=false;
 const deps={wait:async()=>{},token:TOKEN,active:()=>current,store:async v=>Object.assign(state,v),blockHost:async()=>{blocked=true;},scheduler:{next:(_now,lane)=>views.find(v=>v.kind===lane),done:(...x)=>done.push(x),snapshot:()=>({})},request:async(type,body)=>{calls.push({type,...body});if(type==='api_begin')return {job_id:'b'.repeat(32),request:descriptor,view:views[0],expires_at:new Date(Date.now()+60000).toISOString()};return {count:0,status:'partial'};},chrome:{tabs:{query:async()=>[{id:1,url:'https://www.saveticker.com/news#trader-saveticker-api='+TOKEN}],sendMessage:async()=>{if(opts.stop)current=false;return response;}},windows:{update:()=>{throw Error('focus forbidden');}}},...opts.deps};
 return {run:()=>apiCycle(deps),calls,state,done,isBlocked:()=>blocked};
}
test('API cycle never focuses/activates, saves only current job and preserves partial',async()=>{
 const r=runner();await r.run();assert.ok(r.calls.some(c=>c.type==='api_snapshot'));assert.equal(r.state.lane_news,'API 부분 수집 · 0개');
 const stale=runner(undefined,{stop:true});await stale.run();assert.ok(!stale.calls.some(c=>c.type==='api_snapshot'));
});
test('auth stops later lanes; rate limit persists whole host deadline',async()=>{
 const auth=runner({status:403});await auth.run();assert.ok(auth.isBlocked());assert.equal(auth.calls.filter(c=>c.type==='api_begin').length,1);
 const rate=runner({status:429,retry_after:'120'});await rate.run();assert.ok(rate.state.apiCooldownUntil>=Date.now()+119000);assert.equal(rate.calls.filter(c=>c.type==='api_begin').length,1);
});
test('production worker API mode routes to api_snapshot and persisted cooldown prevents tab access',async()=>{
 const ev=()=>({listeners:[],addListener(fn){this.listeners.push(fn);}}), state={enabled:false,backend:'api',ownerToken:TOKEN},calls=[];const messages=ev();
 globalThis.chrome={storage:{local:{get:async()=>({...state}),set:async x=>Object.assign(state,x)}},runtime:{id:'ext',onMessage:messages,onStartup:ev(),onInstalled:ev(),connectNative:()=>{const p={onMessage:ev(),onDisconnect:ev(),disconnect(){},postMessage(m){calls.push(m);queueMicrotask(()=>p.onMessage.listeners[0]({ok:true,id:m.id,views:[{stream:'news_all',kind:'news',url:'https://www.saveticker.com/news',interval_s:30}],job_id:'b'.repeat(32),request:descriptor,status:'partial',count:1}));}};return p;}},tabs:{query:async()=>{calls.push({type:'query'});return[{id:1,url:'https://www.saveticker.com/news#trader-saveticker-api='+TOKEN}];},sendMessage:async()=>({url:URL_NEWS,status:200,content_type:'application/json',data:{news_list:[]}})},alarms:{onAlarm:ev(),create:async()=>{},clear:async()=>{}},downloads:{onChanged:ev()}};
 const worker=await import('../worker.js?api-mode-integration');state.enabled=true;await worker.tick();assert.ok(calls.some(c=>c.type==='api_snapshot'));assert.ok(!calls.some(c=>c.type==='begin'||c.type==='snapshot'));
 state.apiCooldownUntil=Date.now()+120000;calls.length=0;await worker.tick();assert.ok(!calls.some(c=>c.type==='query'||c.type==='api_begin'));
 await new Promise(resolve=>messages.listeners[0]({action:'stop'},{id:'ext'},resolve));
});
test('challenge query on owned tab is reused rather than repeatedly opening windows',async()=>{
 const r=runner({status:403},{deps:{chrome:{tabs:{query:async()=>[{id:1,url:'https://www.saveticker.com/news?__cf_chl_rt_tk=synthetic#trader-saveticker-api='+TOKEN}],sendMessage:async()=>({status:403})},windows:{create:async()=>{throw Error('must not open a new challenge tab');}}}}});
 await r.run();assert.ok(r.isBlocked());
});

test('opaque authentication redirect stops before reading a body',async()=>{
 const c=await content({type:'opaqueredirect',status:0,url:'',headers:new Headers()});
 assert.equal((await c.send()).status,403);
});
test('persisted 429 cooldown survives worker reload and DOM mode switch',async()=>{
 const ev=()=>({listeners:[],addListener(fn){this.listeners.push(fn);}}), state={enabled:false,backend:'dom',apiCooldownUntil:Date.now()+180000},calls=[];
 const messages=ev();globalThis.chrome={storage:{local:{get:async()=>({...state}),set:async x=>Object.assign(state,x)}},runtime:{id:'ext',onMessage:messages,onStartup:ev(),onInstalled:ev(),connectNative:()=>{const p={onMessage:ev(),onDisconnect:ev(),disconnect(){},postMessage(m){calls.push(m);queueMicrotask(()=>p.onMessage.listeners[0]({ok:true,id:m.id,views:[{stream:'news_all',kind:'news',interval_s:30}]}));}};return p;}},tabs:{query:async()=>{throw Error('must not access tabs during cooldown');}},alarms:{onAlarm:ev(),create:async()=>{},clear:async()=>{}},downloads:{onChanged:ev()}};
 const worker=await import('../worker.js?api-cooldown-restart');state.enabled=true;await worker.tick();assert.ok(!calls.some(c=>c.type==='begin'||c.type==='api_begin'));assert.equal(state.connection,'요청 제한 · 서버 대기 시간 적용');await new Promise(resolve=>messages.listeners[0]({action:'stop'},{id:'ext'},resolve));
});
