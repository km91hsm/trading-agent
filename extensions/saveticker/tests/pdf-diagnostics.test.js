import test from 'node:test';
import assert from 'node:assert/strict';
const event = () => { const listeners = []; return { listeners, addListener: f => listeners.push(f) }; };
let nextProbe = 0;
async function scenario({ clickReason, beginError, saveError }) {
    const state = { enabled: false, ownerToken: 'owner', ownedWindowId: 7 }, calls = [], messages = event(), name = 'b'.repeat(32) + '-1234567890123.pdf', view = { stream: 'news_reports', kind: 'news', url: 'https://www.saveticker.com/news', interval_s: 900 }, tab = { id: 8, windowId: 7, url: view.url + '#trader-saveticker=owner:background' };
    const realTimer = setTimeout;
    globalThis.setTimeout = (fn, delay, ...a) => realTimer(fn, delay === 2000 ? 0 : delay, ...a);
    globalThis.chrome = { storage: { local: { get: async () => ({ ...state }), set: async (v) => Object.assign(state, v) } }, runtime: { id: 'ext', onMessage: messages, onStartup: event(), onInstalled: event(), connectNative: () => {
                const p = { onMessage: event(), onDisconnect: event(), disconnect: () => { }, postMessage: m => {
                        calls.push(m);
                        let r = { ok: true, id: m.id, count: 2, status: 'partial' };
                        if (m.type === 'hello')
                            r.views = [view];
                        if (m.type === 'begin')
                            Object.assign(r, { view, job_id: 'job' });
                        if (m.type === 'snapshot')
                            r.report_items = [{ id: 'one', title: 'One', attachment_ok: false }, { id: 'two', title: 'Two', attachment_ok: false }];
                        if (m.type === 'pdf_begin') {
                            const n = calls.filter(x => x.type === 'pdf_begin').length;
                            if (beginError || n > 1)
                                r = { ok: false, id: m.id, error: beginError || 'pdf_busy' };
                            else
                                Object.assign(r, { ticket: 'ticket', download_name: name });
                        }
                        if (m.type === 'pdf' && saveError)
                            r = { ok: false, id: m.id, error: saveError };
                        queueMicrotask(() => p.onMessage.listeners.forEach(f => f(r)));
                    } };
                return p;
            } }, windows: { get: async () => ({ id: 7, state: 'normal' }), update: async () => { } }, tabs: { query: async () => [tab], update: async () => { }, sendMessage: async (_id, m) => m.type === 'collect' ? { page_url: view.url, html: '<body>Reports</body>', selection: { loaded: true }, status: 'partial' } : m.type === 'pdf_release' ? { ok: true } : clickReason ? { ok: false, reason: clickReason } : { ok: true, nonce: m.nonce, observed_url: 'blob:https://www.saveticker.com/report' } }, alarms: { onAlarm: event(), create: async () => { }, clear: async () => { } }, downloads: { onDeterminingFilename: event(), onChanged: event(), download: async () => 42, search: async () => [{ id: 42, state: 'complete', filename: '/Downloads/SaveTicker/' + name }] } };
    const worker = await import('../worker.js?pdfdiagnostic-' + nextProbe++);
    await new Promise(r => realTimer(r, 1));
    state.enabled = true;
    await worker.tick();
    await new Promise(r => messages.listeners[0]({ action: 'stop' }, { id: 'ext' }, r));
    globalThis.setTimeout = realTimer;
    assert.ok(!JSON.stringify(state).includes('PRIVATE'));
    return { state, calls };
}
for (const reason of ['hook_not_ready', 'url_unverified', 'button_unverified', 'candidate_missing'])
    test('first ' + reason + ' survives and requests only one Native PDF ticket', async () => { const r = await scenario({ clickReason: reason }); assert.equal(r.state.pdfState, reason); assert.equal(r.calls.filter(m => m.type === 'pdf_begin').length, 1); });
for (const [code, expected] of [['pdf_busy', 'pdf_begin_busy'], ['unknown_report', 'pdf_begin_unknown_report'], ['pdf_cached', 'pdf_begin_cached'], ['PRIVATE https://secret.test', 'pdf_begin_failed']])
    test('Native PDF begin ' + expected + ' is fixed diagnostic', async () => { const r = await scenario({ beginError: code }); assert.equal(r.state.pdfState, expected); });
for (const [code, expected] of [['invalid_pdf_ticket', 'pdf_save_ticket'], ['invalid_pdf_path', 'pdf_save_path'], ['invalid_pdf', 'pdf_save_invalid'], ['PRIVATE https://secret.test', 'pdf_save_failed']])
    test('Native PDF save ' + expected + ' is fixed diagnostic', async () => { const r = await scenario({ saveError: code }); assert.equal(r.state.pdfState, expected); });
test('unrecognized click reason is sanitized and cannot request a second ticket', async () => { const r = await scenario({ clickReason: 'PRIVATE https://secret.test' }); assert.equal(r.state.pdfState, 'url_unverified'); assert.equal(r.calls.filter(m => m.type === 'pdf_begin').length, 1); });
