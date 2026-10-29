import test from 'node:test';
import assert from 'node:assert/strict';
const event = () => { const listeners = []; return { listeners, addListener: f => listeners.push(f) }; };
async function scenario(mode) { const state = { enabled: false, ownerToken: 'owner', ownedWindowId: 7 }, calls = [], cancelled = [], messages = event(); let nonce, downloads = 0; const view = { stream: 'news_reports', kind: 'news', url: 'https://www.saveticker.com/news', interval_s: 900 }, tab = { id: 8, windowId: 7, url: view.url + '#trader-saveticker=owner:background' }, name = 'b'.repeat(32) + '-1234567890123.pdf'; const realTimer = setTimeout; globalThis.setTimeout = (fn, delay, ...a) => realTimer(fn, delay === 2000 ? 0 : delay, ...a); const abort = (n,fields={},sender={}) => new Promise(reply => messages.listeners[0]({type:'pdf_abort',nonce:n,ownerToken:'owner',...fields},{id:'ext',tab,...sender},reply)); globalThis.chrome = { storage: { local: { get: async () => ({ ...state }), set: async (v) => { Object.assign(state, v); if(v.pdfState==='starting'&&mode==='route'){tab.url='https://example.com/after-navigation';assert.deepEqual(await abort(nonce),{ok:true});}
            if (v.pdfState === 'starting' && mode === 'before') {
                assert.deepEqual(await abort(nonce), { ok: true });
            } } } }, runtime: { id: 'ext', onMessage: messages, onStartup: event(), onInstalled: event(), connectNative: () => { const p = { onMessage: event(), onDisconnect: event(), disconnect: () => { }, postMessage: m => { calls.push(m); let r = { ok: true, id: m.id, count: 1, status: 'partial' }; if (m.type === 'hello')
                r.views = [view]; if (m.type === 'begin')
                Object.assign(r, { view, job_id: 'job' }); if (m.type === 'snapshot')
                r.report_items = [{ id: 'report', title: 'Report', attachment_ok: false }]; if (m.type === 'pdf_begin')
                Object.assign(r, { ticket: 'ticket', download_name: name }); queueMicrotask(() => p.onMessage.listeners.forEach(f => f(r))); } }; return p; } }, windows: { get: async () => ({ id: 7, state: 'normal' }), update: async () => { } }, tabs: { query: async () => [tab], update: async () => { }, sendMessage: async (_id, m) => { if (m.type === 'collect')
            return { page_url: view.url, html: '<body>Report</body>', selection: { loaded: true }, status: 'partial' }; if (m.type === 'pdf_release')
            return { ok: true }; nonce = m.nonce; return { ok: true, nonce, observed_url: 'blob:https://www.saveticker.com/report' }; } }, alarms: { onAlarm: event(), create: async () => { }, clear: async () => { } }, downloads: { onDeterminingFilename: event(), onChanged: event(), cancel: async (id) => cancelled.push(id), download: async () => { downloads++;assert.deepEqual(await abort(nonce,{ownerToken:'wrong'}),{ok:false,error:'pdf_abort_denied'});assert.deepEqual(await abort(nonce,{}, {tab:{...tab,id:99}}),{ok:false,error:'pdf_abort_denied'});assert.deepEqual(await abort(nonce,{}, {id:'other'}),{ok:false,error:'pdf_abort_denied'}); assert.deepEqual(await abort('c'.repeat(32)), { ok: false, error: 'pdf_abort_denied' }); if (mode === 'during') {
            return new Promise(resolve => queueMicrotask(async () => { assert.deepEqual(await abort(nonce), { ok: true }); resolve(42); }));
        } return mode === 'stale' ? 41 + downloads : 42; }, search: async ({ id }) => [{ id, state: mode === 'stale' ? 'in_progress' : 'complete', filename: '/Downloads/SaveTicker/' + name }] } }; const worker = await import('../worker.js?abort-' + mode); await new Promise(r => realTimer(r, 1)); state.enabled = true; await worker.tick(); if (mode === 'stale') {
    let resolveOld;
    chrome.downloads.search = ({ id }) => id === 42 ? new Promise(r => resolveOld = r) : Promise.resolve([{ id, state: 'in_progress', filename: '/Downloads/SaveTicker/' + name }]);
    const old = chrome.downloads.onChanged.listeners[0]({ id: 42, state: { current: 'complete' } });
    await new Promise(r => realTimer(r, 1));
    await new Promise(r => messages.listeners[0]({ action: 'stop' }, { id: 'ext' }, r));
    const now = Date.now;
    Date.now = () => now() + 901000;
    state.enabled = true;
    await worker.tick();
    Date.now = now;
    resolveOld([{ id: 42, state: 'complete', filename: '/Downloads/SaveTicker/' + name }]);
    await old;
    assert.equal(state.pdfState, 'downloading', 'stale completion leaves new pending intact');
    assert.equal(calls.filter(m => m.type === 'pdf').length, 0);
    chrome.downloads.search = async ({ id }) => [{ id, state: 'complete', filename: '/Downloads/SaveTicker/' + name }];
    await chrome.downloads.onChanged.listeners[0]({ id: 43, state: { current: 'complete' } });
} await new Promise(r => messages.listeners[0]({ action: 'stop' }, { id: 'ext' }, r)); assert.deepEqual(await abort(nonce),{ok:false,error:'pdf_abort_denied'},'stop invalidates prior epoch/proof');globalThis.setTimeout = realTimer; return { state, calls, cancelled, downloads }; }
test('late MAIN abort after candidate reply prevents download initiation', { timeout: 2500 }, async () => { const r = await scenario('before'); assert.equal(r.downloads, 0); assert.deepEqual(r.cancelled, []); assert.equal(r.calls.filter(m => m.type === 'pdf').length, 0); assert.equal(r.state.pdfState, 'handoff_aborted'); });
test('abort during unresolved download API cancels only returned owned id and skips Native', { timeout: 2500 }, async () => { const r = await scenario('during'); assert.equal(r.downloads, 1); assert.deepEqual(r.cancelled, [42]); assert.equal(r.calls.filter(m => m.type === 'pdf').length, 0); assert.equal(r.state.pdfState, 'handoff_aborted'); });
test('unrelated nonce cannot cancel direct download', async () => { const r = await scenario('wrong'); assert.equal(r.downloads, 1); assert.deepEqual(r.cancelled, []); assert.equal(r.calls.filter(m => m.type === 'pdf').length, 1); assert.equal(r.state.pdfState, 'saved'); });
test('stale completion after stop/restart cannot consume or clear a newer pending PDF', async () => { const r = await scenario('stale'); assert.equal(r.downloads, 2); assert.equal(r.calls.filter(m => m.type === 'pdf').length, 1); assert.equal(r.state.pdfState, 'saved'); });
test('route change cannot prevent authenticated cancellation of the bound PDF handoff',async()=>{const r=await scenario('route');assert.equal(r.downloads,0);assert.equal(r.calls.filter(m=>m.type==='pdf').length,0);assert.equal(r.state.pdfState,'handoff_aborted');});
