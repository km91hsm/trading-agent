import test from 'node:test';
import assert from 'node:assert/strict';
const event = () => { const listeners = []; return { listeners, addListener: f => listeners.push(f), emit: (...a) => listeners.forEach(f => f(...a)) }; };
test('pendingUrl-only new tabs collect; failed commands consume job and recover without poisoning native connection', async () => {
    const state = { enabled: false, ownerToken: 'owner' }, calls = [], tabs = [], jobs = new Map();
    let listener, failContent = true, forceBusy = false, navigations = 0;
    const realTimer = setTimeout, realNow = Date.now;
    globalThis.setTimeout = (fn, delay, ...a) => realTimer(fn, (delay === 500 || delay === 2000) ? 0 : delay, ...a);
    const view = { stream: 'market_gain_live', kind: 'market', url: 'https://www.saveticker.com/watchlist', tab: '상승', regular: false, interval_s: 30 };
    const onMessage = event();
    globalThis.chrome = { storage: { local: { get: async () => ({ ...state }), set: async (v) => Object.assign(state, v) } }, runtime: { id: 'ext', onMessage, onStartup: event(), onInstalled: event(), connectNative: () => {
                const p = { onMessage: event(), onDisconnect: event(), disconnect: () => { }, postMessage: m => {
                        calls.push(m);
                        let r = { ok: true, id: m.id, count: 1, status: 'partial' };
                        if (m.type === 'hello')
                            r = { ...r, views: [view] };
                        if (m.type === 'begin') {
                            if (forceBusy || jobs.has(m.stream))
                                r = { id: m.id, ok: false, error: 'lane_busy' };
                            else {
                                jobs.set(m.stream, 'j');
                                r = { ...r, job_id: 'j', view };
                            }
                        }
                        if (m.type === 'snapshot')
                            jobs.delete(m.stream);
                        queueMicrotask(() => p.onMessage.emit(r));
                    } };
                return p;
            } }, windows: { update:async()=>{},get: async (id) => ({ id, state: 'normal' }), create: async ({ url }) => { tabs.push({ id: 1, pendingUrl: url, windowId: 7 }); return { id: 7, state: 'normal', tabs: [{ id: 1, windowId: 7 }] }; } }, alarms: { onAlarm: event(), create: async () => { }, clear: async () => { } }, tabs: { query: async () => tabs, create: async ({ url }) => { const t = { id: 1, pendingUrl: url }; tabs.push(t); return { id: t.id }; }, move: async () => { }, update: async (id, opts) => { if (opts.url)
                navigations++; }, remove: async () => { }, sendMessage: async () => {
                if (failContent)
                    throw Error('PRIVATE PAGE CONTENT MUST NOT BE LOGGED');
                return { page_url: view.url, html: '<body>rank</body>', selection: { loaded: true, tab: '상승', regular: false }, status: 'partial' };
            } }, downloads: { onDeterminingFilename: event(), onChanged: event(), search: async () => [] } };
    const worker = await import('../worker.js?startup');
    await new Promise(r => realTimer(r, 1));
    state.enabled = true;
    await worker.tick();
    assert.equal(calls.filter(m => m.type === 'snapshot' && m.status === 'error').length, 1, 'failed command consumes issued job');
    assert.equal(jobs.size, 0);
    assert.equal(navigations, 0, 'new window tab without URL safely uses its explicitly requested page');
    assert.equal(state.lane_error_market, 'content_unavailable');
    assert.equal(state.connection, '연결됨');
    assert.ok(!JSON.stringify(state).includes('PRIVATE'));
    failContent = false;
    Date.now = () => realNow() + 31000;
    await worker.tick();
    assert.equal(calls.filter(m => m.type === 'begin').length, 1, 'failed stream stays quiet through its backoff');
    Date.now = () => realNow() + 61000;
    await worker.tick();
    assert.equal(calls.filter(m => m.type === 'snapshot' && m.status === 'partial').length, 1, 'pendingUrl-only tab recovers and saves');
    assert.ok(state.lastCollected);
    forceBusy = true;
    Date.now = () => realNow() + 92000;
    await worker.tick();
    assert.equal(state.lane_error_market, 'lane_busy');
    assert.equal(state.connection, '연결됨');
    await new Promise(resolve => onMessage.listeners[0]({ action: 'stop' }, { id: 'ext' }, resolve));
    Date.now = realNow;
    globalThis.setTimeout = realTimer;
});
