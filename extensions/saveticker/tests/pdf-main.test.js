import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from '../../../web/node_modules/jsdom/lib/api.js';
const nonce = 'a'.repeat(32), owner = 'b'.repeat(32), script = () => fs.readFileSync(new URL('../pdf-main.js', import.meta.url), 'utf8');
function fixture() {
    const dom = new JSDOM('<body><button aria-label="PDF 다운로드"></button></body>', { url: 'https://www.saveticker.com/news?label_group=7#trader-saveticker=' + owner + ':background', runScripts: 'outside-only' }), w = dom.window, messages = [], revoked = [], clicked = [];
    w.postMessage = data => messages.push(data);
    w.URL.revokeObjectURL = url => revoked.push(url);
    w.HTMLAnchorElement.prototype.click = function () { clicked.push(this.href); };
    const handlers = {};
    const add = w.document.addEventListener.bind(w.document);
    w.document.addEventListener = (name, handler, ...args) => { handlers[name] = handler; add(name, handler, ...args); };
    w.eval(script());
    const send = (type, n = nonce, origin = w.location.origin, source = w) => {
        if (type === 'arm')
            w.document.querySelector('button').setAttribute('data-trader-pdf-nonce', n);
        return w.dispatchEvent(new w.MessageEvent('message', { data: { channel: 'trader-saveticker-pdf', type, nonce: n }, origin, source }));
    };
    const anchor = url => { const a = w.document.createElement('a'); a.href = url; return a; };
    return { dom, w, messages, revoked, clicked, send, anchor, handlers };
}
test('dormant hook leaves detached anchors unchanged, wrong origin/nonce cannot arm or release', () => { const f = fixture(), url = 'blob:https://www.saveticker.com/one'; f.anchor(url).click(); assert.deepEqual(f.clicked, [url]); f.send('arm', nonce, 'https://evil.test'); f.anchor(url).click(); assert.equal(f.clicked.length, 2); f.send('arm'); assert.equal(f.messages.at(-1).type, 'ready'); f.send('release', 'c'.repeat(32)); f.w.document.querySelector('button').click(); f.anchor(url).click(); assert.equal(f.clicked.length, 2); assert.equal(f.messages.at(-1).type, 'candidate'); f.dom.window.close(); });
test('armed detached blob click is suppressed; captured revoke deferred until matching release restores hook', () => { const f = fixture(), url = 'blob:https://www.saveticker.com/report'; f.send('arm'); f.w.document.querySelector('button').click(); f.anchor(url).click(); assert.equal(f.clicked.length, 0); assert.equal(f.messages.at(-1).url, url); f.w.URL.revokeObjectURL(url); assert.deepEqual(f.revoked, []); f.w.URL.revokeObjectURL('blob:https://www.saveticker.com/other'); assert.equal(f.revoked.length, 1); f.send('release'); assert.equal(f.revoked.at(-1), url); f.anchor(url).click(); assert.equal(f.clicked.length, 1); f.dom.window.close(); });
test('multiple candidates abort and every wrapped call enforces hard deadline', () => { const f = fixture(), url = 'blob:https://www.saveticker.com/report'; f.send('arm'); f.w.document.querySelector('button').click(); f.anchor(url).click(); f.anchor(url + '2').click(); assert.equal(f.messages.at(-1).type, 'aborted'); assert.equal(f.clicked.length, 0); f.send('arm', 'd'.repeat(32)); const old = f.w.Date.now; f.w.Date.now = () => old() + 16000; f.anchor(url).click(); assert.equal(f.clicked.length, 1); assert.equal(f.messages.at(-1).reason, 'expired'); f.w.Date.now = old; f.dom.window.close(); });
test('trusted interaction and navigation abort restore normal detached clicks', () => { const f = fixture(), url = 'blob:https://www.saveticker.com/report'; f.send('arm'); f.handlers.pointerdown({ isTrusted: true }); assert.equal(f.messages.at(-1).reason, 'user_interaction'); f.anchor(url).click(); assert.equal(f.clicked.length, 1); f.send('arm'); f.w.dispatchEvent(new f.w.Event('pagehide')); assert.equal(f.messages.at(-1).reason, 'navigation'); f.anchor(url).click(); assert.equal(f.clicked.length, 2); f.dom.window.close(); });
test('arm requires exact marked button and source window; unrelated HTTPS click stays untouched', () => { const f = fixture(), url = 'https://www.saveticker.com/file.pdf'; f.send('arm', nonce, f.w.location.origin, {}); f.anchor(url).click(); assert.equal(f.messages.length, 0); f.w.document.querySelector('button').removeAttribute('data-trader-pdf-nonce'); f.w.dispatchEvent(new f.w.MessageEvent('message', { data: { channel: 'trader-saveticker-pdf', type: 'arm', nonce }, origin: f.w.location.origin, source: f.w })); assert.equal(f.messages.length, 0); f.send('arm'); f.w.document.querySelector('button').click(); f.anchor(url).click(); assert.equal(f.clicked.length, 2); assert.equal(f.messages.at(-1).type, 'ready'); f.send('release'); f.dom.window.close(); });
test('blob anchors before the exact marked PDF button click stay untouched', () => { const f = fixture(), url = 'blob:https://www.saveticker.com/before'; f.send('arm'); f.anchor(url).click(); assert.equal(f.clicked.length, 1); assert.equal(f.messages.at(-1).type, 'ready'); f.w.document.querySelector('button').click(); f.anchor(url + '-report').click(); assert.equal(f.clicked.length, 1); assert.equal(f.messages.at(-1).type, 'candidate'); f.send('release'); f.dom.window.close(); });
