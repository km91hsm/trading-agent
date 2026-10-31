// Regression fixtures for demonstrated capability gaps; the real site's
// internal download mechanism has not been identified by these tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from '../../../web/node_modules/jsdom/lib/api.js';
const owner = 'b'.repeat(32), nonce = 'a'.repeat(32), title = '2026년 10월 29일 (목요일)', main = fs.readFileSync(new URL('../pdf-main.js', import.meta.url), 'utf8'), content = fs.readFileSync(new URL('../content.js', import.meta.url), 'utf8'), url = 'https://www.saveticker.com/news?label_group=7#trader-saveticker=' + owner + ':background';
function fixture(html) { const dom = new JSDOM(html, { url, runScripts: 'outside-only' }), w = dom.window, messages = [], revoked = []; w.setTimeout = () => 0; w.URL.revokeObjectURL = value => revoked.push(value); w.postMessage = data => { messages.push(data); w.dispatchEvent(new w.MessageEvent('message', { data, origin: w.location.origin, source: w })); }; w.eval(main); return { dom, w, messages, revoked }; }
for (const attached of [false, true])
    test('armed hook captures ' + (attached ? 'attached' : 'detached') + ' synthetic MouseEvent dispatch and defers revoke', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
        f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
        f.w.document.querySelector('button').click();
        const a = f.w.document.createElement('a');
        a.href = 'blob:https://www.saveticker.com/dispatch';
        a.download = 'report.pdf';
        if (attached)
            f.w.document.body.append(a);
        let received = 0;
        a.addEventListener('click', event => { received++; event.preventDefault(); });
        const event = new f.w.MouseEvent('click', { bubbles: true, cancelable: true });
        assert.equal(a.dispatchEvent(event), false);
        f.w.URL.revokeObjectURL(a.href);
        assert.equal(received, 0);
        assert.equal(event.defaultPrevented, true);
        assert.equal(f.messages.filter(m => m.type === 'candidate').length, 1);
        assert.deepEqual(f.revoked, []);
        f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'release', nonce });
        assert.deepEqual(f.revoked, [a.href]);
    }
    finally {
        f.dom.window.close();
    } });
test('dispatch hook is dormant before marked button and restores inherited descriptors exactly', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
    const p = f.w.HTMLAnchorElement.prototype, before = Object.getOwnPropertyDescriptor(p, 'dispatchEvent'), clickBefore = Object.getOwnPropertyDescriptor(p, 'click'), a = f.w.document.createElement('a');
    a.href = 'blob:https://www.saveticker.com/before';
    let count = 0;
    a.addEventListener('click', e => { count++; e.preventDefault(); });
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    a.dispatchEvent(new f.w.MouseEvent('click', { cancelable: true }));
    assert.equal(count, 1);
    assert.equal(f.messages.filter(m => m.type === 'candidate').length, 0);
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'release', nonce });
    assert.deepEqual(Object.getOwnPropertyDescriptor(p, 'dispatchEvent'), before);
    assert.deepEqual(Object.getOwnPropertyDescriptor(p, 'click'), clickBefore);
}
finally {
    f.dom.window.close();
} });
test('stale dispatch wrapper cannot capture and release preserves another installed wrapper', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
    const p = f.w.HTMLAnchorElement.prototype;
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    f.w.document.querySelector('button').click();
    const stale = p.dispatchEvent;
    const other = function (e) { return stale.call(this, e); };
    p.dispatchEvent = other;
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'release', nonce });
    assert.equal(p.dispatchEvent, other);
    const a = f.w.document.createElement('a');
    a.href = 'blob:https://www.saveticker.com/stale';
    let count = 0;
    a.addEventListener('click', e => { count++; e.preventDefault(); });
    a.dispatchEvent(new f.w.MouseEvent('click', { cancelable: true }));
    assert.equal(count, 1);
    assert.equal(f.messages.filter(m => m.type === 'candidate').length, 0);
}
finally {
    f.dom.window.close();
} });
async function buttonsCase(shape) { const button = (id, hidden = false, disabled = false) => '<div' + (hidden ? ' style="display:none"' : '') + '><button id="' + id + '" aria-label="PDF 다운로드"' + (disabled ? ' disabled' : '') + '>PDF</button></div>'; const hidden = button('hidden', true), visible = button('visible'); const cards = shape === 'separate' ? '<article><p>' + title + '</p>' + hidden + '</article><article><p>' + title + '</p>' + visible + '</article>' : '<article><p>' + title + '</p>' + (shape === 'ambiguous' ? button('other') : shape === 'disabled' ? button('disabled', false, true) : hidden) + visible + '</article>'; const f = fixture('<button aria-label="프로필 메뉴"></button>' + cards); let listener; const clicks = []; f.w.chrome = { runtime: { id: 'ext', onMessage: { addListener: fn => listener = fn }, sendMessage: async () => ({ ok: true }) } }; for (const b of f.w.document.querySelectorAll('[aria-label="PDF 다운로드"]'))
    b.addEventListener('click', () => { clicks.push(b.id); const a = f.w.document.createElement('a'); a.href = 'blob:https://www.saveticker.com/' + b.id; a.click(); }); f.w.eval(content); try {
    const reply = await new Promise(resolve => listener({ type: 'pdf_click', token: owner, nonce, title }, { id: 'ext' }, resolve));
    return { reply, clicks, ready: f.messages.some(m => m.type === 'ready') };
}
finally {
    f.dom.window.close();
} }
for (const shape of ['separate', 'same-card', 'disabled'])
    test('unique visible enabled PDF selection ignores ' + shape + ' duplicates', async () => { const r = await buttonsCase(shape); assert.equal(r.reply.ok, true); assert.deepEqual(r.clicks, ['visible']); });
test('two visible matching PDF buttons fail closed before arm', async () => { const r = await buttonsCase('ambiguous'); assert.equal(r.reply.ok, false); assert.equal(r.reply.reason, 'button_unverified'); assert.deepEqual(r.clicks, []); assert.equal(r.ready, false); });
test('noncancelable synthetic MouseEvent dispatch is captured without calling original event', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
    const base = f.w.EventTarget.prototype.dispatchEvent;
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    assert.equal(f.w.EventTarget.prototype.dispatchEvent, base);
    f.w.document.querySelector('button').click();
    const a = f.w.document.createElement('a');
    a.href = 'blob:https://www.saveticker.com/default-mouseevent';
    let count = 0;
    a.addEventListener('click', () => count++);
    assert.equal(a.dispatchEvent(new f.w.MouseEvent('click')), true);
    assert.equal(count, 0);
    assert.equal(f.messages.filter(m => m.type === 'candidate').length, 1);
}
finally {
    f.dom.window.close();
} });
test('dispatch passthrough keeps other origins, event types and non-MouseEvent clicks unchanged', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    f.w.document.querySelector('button').click();
    let count = 0;
    for (const [href, event] of [['https://www.saveticker.com/file.pdf', new f.w.MouseEvent('click', { cancelable: true })], ['blob:https://other.test/file', new f.w.MouseEvent('click', { cancelable: true })], ['blob:https://www.saveticker.com/file', new f.w.MouseEvent('mouseup')], ['blob:https://www.saveticker.com/file', new f.w.Event('click', { cancelable: true })]]) {
        const a = f.w.document.createElement('a');
        a.href = href;
        a.addEventListener(event.type, e => { count++; e.preventDefault(); });
        a.dispatchEvent(event);
    }
    assert.equal(count, 4);
    assert.equal(f.messages.filter(m => m.type === 'candidate').length, 0);
}
finally {
    f.dom.window.close();
} });
test('dispatch capture shares multiple-candidate abort and expiration cleanup guards', () => { const f = fixture('<button aria-label="PDF 다운로드" data-trader-pdf-nonce="' + nonce + '">PDF</button>'); try {
    const p = f.w.HTMLAnchorElement.prototype, descriptor = Object.getOwnPropertyDescriptor(p, 'dispatchEvent');
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    f.w.document.querySelector('button').click();
    const first = f.w.document.createElement('a');
    first.href = 'blob:https://www.saveticker.com/first';
    first.dispatchEvent(new f.w.MouseEvent('click', { cancelable: true }));
    f.w.URL.revokeObjectURL(first.href);
    const second = f.w.document.createElement('a');
    second.href = 'blob:https://www.saveticker.com/second';
    assert.equal(second.dispatchEvent(new f.w.MouseEvent('click', { cancelable: true })), false);
    assert.equal(f.messages.at(-1).reason, 'multiple_candidates');
    assert.deepEqual(f.revoked, [first.href]);
    assert.deepEqual(Object.getOwnPropertyDescriptor(p, 'dispatchEvent'), descriptor);
    f.w.postMessage({ channel: 'trader-saveticker-pdf', type: 'arm', nonce });
    f.w.document.querySelector('button').click();
    const now = f.w.Date.now;
    f.w.Date.now = () => now() + 16000;
    let received = 0;
    second.addEventListener('click', e => { received++; e.preventDefault(); });
    second.dispatchEvent(new f.w.MouseEvent('click', { cancelable: true }));
    assert.equal(received, 1);
    assert.equal(f.messages.at(-1).reason, 'expired');
    assert.deepEqual(Object.getOwnPropertyDescriptor(p, 'dispatchEvent'), descriptor);
}
finally {
    f.dom.window.close();
} });
