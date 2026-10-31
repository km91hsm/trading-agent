import test from 'node:test';
import assert from 'node:assert/strict';
import { Scheduler, marker, ownedLane, PdfPair, safePage, sanitizeTree } from '../core.js';
test('ownership never adopts ordinary tabs', () => { assert.equal(ownedLane({ url: 'https://www.saveticker.com/news' }, 'abc'), null); assert.equal(ownedLane({ url: marker('https://www.saveticker.com/news', 'abc', 'news') }, 'abc'), 'news'); assert.equal(ownedLane({ url: marker('https://www.saveticker.com/news', 'other', 'news') }, 'abc'), null); assert.equal(safePage('https://www.saveticker.com/news#secret'), 'https://www.saveticker.com/news'); assert.throws(() => safePage('https://evil.com/news')); });
test('all native plan streams rotate and cold work never starves', () => {
    const s = new Scheduler();
    const views = Array.from({ length: 20 }, (_, i) => ({ stream: `v${i}`, kind: "news", interval_s: i < 10 ? 30 : 300 }));
    s.plan(views);
    const got = [];
    for (let i = 0; i < 20; i++) {
        const v = s.next(1000000);
        got.push(v.stream);
        s.done(v, 1000000);
    }
    assert.equal(new Set(got).size, 20);
    s.block('news');
    assert.equal(s.blocked.has('news'), true);
    s.resume();
    assert.equal(s.blocked.size, 0);
});
test('PDF pairing requires before-click ticket, referrer, time, single id and exact path', () => { const p = new PdfPair(); assert.equal(p.claim({ id: 1 }), null); p.begin({ ticket: 't', download_name: 'a'.repeat(32) + '-1234567890123.pdf', item_id: 'i' }, 1000); assert.equal(p.claim({ id: 1, startTime: new Date(1100).toISOString(), referrer: 'https://evil.com/news', mime: 'application/pdf' }), null); p.expect('blob:https://www.saveticker.com/expected'); assert.ok(p.claim({ url: 'blob:https://www.saveticker.com/expected', id: 2, startTime: new Date(1100).toISOString(), referrer: 'https://www.saveticker.com/news', mime: 'application/pdf' })); assert.equal(p.claim({ id: 3 }), null); assert.equal(p.complete({ id: 2, state: 'complete', filename: '/Downloads/SaveTicker/wrong.pdf' }), null); assert.ok(p.complete({ id: 2, state: 'complete', filename: '/Downloads/SaveTicker/' + p.pending.download_name })); assert.equal(p.pending, null); });
test('projector strips secrets and bounds body nesting', () => { const x = sanitizeTree({ id: 'a', title: 'ok', cookie: 'secret', requestHeaders: { authorization: 'secret' }, content: { text: 'body', password: 'secret' }, regular: { price: 2 }, junk: 3 }); assert.deepEqual(x, { id: 'a', title: 'ok', content: { text: 'body' }, regular: { price: 2 } }); });
test('ownership markers must be exact, not prefixes or suffixes', () => { assert.equal(ownedLane({ url: 'https://www.saveticker.com/news#prefixtrader-saveticker=abc:news' }, 'abc'), null); assert.equal(ownedLane({ url: 'https://www.saveticker.com/news#trader-saveticker=abc:news:suffix' }, 'abc'), null); });
test('full production plan preserves every lane, regular/live mode, and five months', () => {
    const views = [{ stream: 'news_all', kind: 'news', interval_s: 30 }, { stream: 'news_reports', kind: 'news', interval_s: 900 }, { stream: 'community_top5', kind: 'community', interval_s: 30 }];
    for (const kind of ['community', 'market'])
        for (const tab of kind === 'market' ? ['turnover', 'gain', 'loss'] : ['index', 'gain', 'loss'])
            for (const regular of [false, true])
                views.push({ stream: `${kind}_${tab}_${regular ? 'regular' : 'live'}`, kind, regular, interval_s: regular ? 300 : 30 });
    for (const month of ['2026-09', '2026-10', '2026-11', '2026-12', '2027-01'])
        views.push({ stream: 'calendar_' + month, kind: 'calendar', month, interval_s: 300 });
    const scheduler = new Scheduler();
    scheduler.plan(views);
    const collected = [];
    for (let i = 0; i < views.length; i++) {
        const view = scheduler.next(1000000);
        assert.ok(view);
        collected.push(view.stream);
        scheduler.done(view, 1000000);
    }
    assert.deepEqual(new Set(collected), new Set(views.map(v => v.stream)));
    assert.equal(scheduler.next(1000000), undefined);
});
test('PDF cannot claim generic concurrent download without exact observed URL', () => { const p = new PdfPair(); p.begin({ ticket: 't', download_name: 'a'.repeat(32) + '-1234567890123.pdf', item_id: 'i' }, 1000); const item = { id: 4, startTime: new Date(1100).toISOString(), referrer: 'https://www.saveticker.com/news', mime: 'application/pdf', url: 'blob:https://www.saveticker.com/other' }; assert.equal(p.claim(item), null); p.expect('blob:https://www.saveticker.com/expected'); assert.equal(p.claim(item), null); assert.ok(p.claim({ ...item, url: 'blob:https://www.saveticker.com/expected' })); });
test('failed report retains normal interval and never starves untouched months', () => {
 const s=new Scheduler(), report={stream:'news_reports',kind:'news',interval_s:900}, month={stream:'calendar_2026-10',kind:'calendar',interval_s:300};
 s.plan([report,month]);s.done(report,100000,true);
 assert.equal(s.next(130000).stream,month.stream);s.done(month,130000);
 assert.equal(s.next(130000),undefined);s.plan([report]);assert.equal(s.next(999999,'background'),undefined);
 assert.equal(s.next(1000000).stream,report.stream);
});
test('failure deadline dominates normal due time, grows boundedly and success resets streak',()=>{
 const s=new Scheduler(),v={stream:'news_all',kind:'news',interval_s:30};s.plan([v]);
 s.done(v,0,true);assert.equal(s.next(30000),undefined);assert.equal(s.next(60000),v);
 s.done(v,60000,true);assert.equal(s.next(90000),undefined);assert.equal(s.next(179999),undefined);assert.equal(s.next(180000),v);
 let at=180000;
 for(let n=0;n<10;n++){s.done(v,at,true);const deadline=s.retryAt.get(v.stream);assert.ok(deadline-at<=900000);assert.equal(s.next(deadline-1),undefined);at=deadline;}
 s.done(v,at,false);assert.equal(s.next(at+30000),v);
 s.done(v,at+30000,true);assert.equal(s.retryAt.get(v.stream),at+90000,'success clears previous failure streak');
 const slow={stream:'news_reports',kind:'news',interval_s:1800};s.plan([slow]);s.done(slow,0,true);assert.equal(s.retryAt.get(slow.stream),1800000,'cap never shortens a normal interval');
});
test('direct PDF mode binds returned download id and ignores generic concurrent downloads', () => { const p = new PdfPair(); p.begin({ ticket: 't', download_name: 'a'.repeat(32) + '-1234567890123.pdf', item_id: 'i', direct: true }); p.expect('blob:https://www.saveticker.com/report'); assert.equal(p.claim({ id: 9, url: 'blob:https://www.saveticker.com/report', referrer: 'https://www.saveticker.com/news', startTime: new Date().toISOString(), mime: 'application/pdf' }), null); p.bind(42); assert.equal(p.complete({ id: 9, state: 'complete', filename: '/Downloads/SaveTicker/' + p.pending.download_name }), null); assert.ok(p.complete({ id: 42, state: 'complete', filename: '/Downloads/SaveTicker/' + p.pending.download_name })); });
test('validated schedule persists normal cooldown and failure streak across worker restart',()=>{
 const v={stream:'news_all',kind:'news',interval_s:30},s=new Scheduler();s.plan([v]);
 s.done(v,100000,true);s.done(v,160000,true);
 const restored=new Scheduler();restored.plan([v]);restored.restore(s.snapshot(),170000);
 assert.equal(restored.next(200000),undefined);assert.equal(restored.next(279999),undefined);assert.equal(restored.next(280000),v);
 restored.done(v,280000,true);assert.equal(restored.retryAt.get(v.stream),520000,'third failure retains streak');
 restored.done(v,520000,false);
 const success=new Scheduler();success.plan([v]);success.restore(restored.snapshot(),525000);
 assert.equal(success.next(549999),undefined);assert.equal(success.next(550000),v);
 success.done(v,550000,true);assert.equal(success.retryAt.get(v.stream),610000,'persisted success clears streak');
});
test('schedule restore rejects unknown streams, malformed times and excessive deadlines',()=>{
 const v={stream:'news_all',kind:'news',interval_s:30},now=1000000;
 for(const invalid of [null,{},Infinity,NaN,-1,1000001]){
  const s=new Scheduler();s.plan([v]);s.restore({version:1,entries:[{stream:v.stream,at:invalid}]},now);assert.equal(s.next(now),v);
 }
 for(const row of [
  {stream:'unknown',at:now},
  {stream:v.stream,at:now,retry_at:Infinity,failure_streak:1},
  {stream:v.stream,at:now,retry_at:now+900001,failure_streak:1},
  {stream:v.stream,at:now,retry_at:now+60000,failure_streak:NaN},
  {stream:v.stream,at:now,retry_at:now+1000,failure_streak:1},
  {stream:v.stream,at:now,retry_at:now+45000,failure_streak:1},
  {stream:v.stream,at:now,retry_at:now+60000,failure_streak:999},
 ]){const s=new Scheduler();s.plan([v]);s.restore({version:1,entries:[row]},now);assert.equal(s.next(now),v);assert.equal(s.times.size,0);}
 const s=new Scheduler();s.plan([v]);s.done(v,now);s.plan([]);assert.deepEqual(s.snapshot(),{version:1,entries:[]});
});
