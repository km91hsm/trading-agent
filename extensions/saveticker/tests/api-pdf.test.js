import test from 'node:test';
import assert from 'node:assert/strict';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn);}});
for (const mode of ['success','late_stop','cross_source']) test('API PDF ticket/ID ownership '+mode,async()=>{
 const timer=globalThis.setTimeout;globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms===2000?0:ms,...args);
 let releaseDownload;
 const token='a'.repeat(32),ticket='b'.repeat(32),name='c'.repeat(32)+'-1234567890123.pdf',calls=[],messages=event();
 const state={enabled:false,backend:'api',ownerToken:token};
 const reportUrl='https://www.saveticker.com/api/report/list?page=1&page_size=20';
 const detailUrl='https://www.saveticker.com/api/report/detail?id=7';
 const pdfUrl='https://www.saveticker.com/api/report/download?url=%2Fapi%2Fuploads%2Fdocuments%2F77';
 const view={stream:'news_reports',kind:'news',url:'https://www.saveticker.com/news',interval_s:900};
 const job=(url,kind)=>({job_id:ticket,expires_at:new Date(Date.now()+120000).toISOString(),request:{method:'GET',url,kind,max_bytes:kind==='pdf'?25*1024*1024:2097152}});
 globalThis.chrome={storage:{local:{get:async()=>({...state}),set:async r=>Object.assign(state,r)}},runtime:{id:'ext',onMessage:messages,onInstalled:event(),onStartup:event(),connectNative:()=>{const p={onMessage:event(),onDisconnect:event(),disconnect(){},postMessage(m){calls.push(m);let r={id:m.id,ok:true,status:'partial',count:1};if(m.type==='hello')r.views=[view];if(m.type==='api_begin')Object.assign(r,job(reportUrl,'reports'));if(m.type==='api_prepare')r.requests=[job(detailUrl,'report_detail')];if(m.type==='api_snapshot')r.pdf_items=[{id:'report'}];if(m.type==='api_pdf_begin')Object.assign(r,job(pdfUrl,'pdf'),{ticket,download_name:name});queueMicrotask(()=>p.onMessage.listeners.forEach(f=>f(r)));}};return p;}},tabs:{query:async()=>[{id:1,url:'https://www.saveticker.com/news#trader-saveticker-api='+token}],sendMessage:async(_id,m)=>{calls.push({type:'content',message:m});if(m.type==='api_pdf_release'||m.type==='api_abort')return{ok:true};return {status:200,url:m.job.request.url,content_type:m.job.request.kind==='pdf'?'application/pdf':'application/json',data:{},...(m.job.request.kind==='pdf'?{blob_url:(mode==='cross_source'?'blob:https://evil.invalid/foreign':'blob:https://www.saveticker.com/own'),nonce:ticket}:{})};}},alarms:{onAlarm:event(),create:async()=>{},clear:async()=>{}},downloads:{onChanged:event(),download:async options=>{calls.push({type:'download',options});if(mode==='late_stop')return new Promise(resolve=>{releaseDownload=resolve;});return 7;},search:async ({id})=>{assert.equal(id,7);return [{id:7,state:'complete',filename:'/Downloads/SaveTicker/'+name}];},cancel:async id=>{calls.push({type:'cancel',id});}}};
 const worker=await import('../worker.js?api-pdf-'+mode);state.enabled=true;const running=worker.tick();
 if(mode==='late_stop'){while(!releaseDownload)await new Promise(r=>timer(r,1));await new Promise(resolve=>messages.listeners[0]({action:'stop'},{id:'ext'},resolve));releaseDownload(7);await running;assert.ok(calls.some(x=>x.type==='cancel'&&x.id===7));assert.ok(!calls.some(x=>x.type==='pdf'));globalThis.setTimeout=timer;return;}
 await running;
 if(mode==='cross_source'){assert.ok(!calls.some(x=>x.type==='download'||x.type==='pdf'));assert.equal(state.pdfState,'api_failed');await new Promise(resolve=>messages.listeners[0]({action:'stop'},{id:'ext'},resolve));globalThis.setTimeout=timer;return;}
 assert.equal(calls.filter(x=>x.type==='download').length,1);
 assert.deepEqual(calls.find(x=>x.type==='download').options,{url:'blob:https://www.saveticker.com/own',filename:'SaveTicker/'+name,conflictAction:'uniquify',saveAs:false});
 assert.deepEqual(calls.find(x=>x.type==='pdf'),{id:calls.find(x=>x.type==='pdf').id,type:'pdf',item_id:'report',ticket,download_name:name});
 assert.equal(state.pdfState,'saved');
 assert.ok(calls.some(x=>x.type==='content'&&x.message.type==='api_pdf_release'));
 await new Promise(resolve=>messages.listeners[0]({action:'stop'},{id:'ext'},resolve));globalThis.setTimeout=timer;
});
