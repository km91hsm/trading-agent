import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from '../../../web/node_modules/jsdom/lib/api.js';

test('popup restores resume after start and repeated resume, and identifies cached worker', async () => {
    const dom = new JSDOM(fs.readFileSync(new URL('../popup.html', import.meta.url), 'utf8'), { runScripts: 'outside-only' });
    const state = { enabled: false }, actions = [];
    dom.window.chrome = {
        storage: { local: { get: async () => ({ ...state }) }, onChanged: { addListener() {} } },
        runtime: { sendMessage: async ({ action }) => { actions.push(action); state.enabled = action !== 'stop'; } }
    };
    const settle = () => new Promise(resolve => setTimeout(resolve, 0));
    dom.window.eval(fs.readFileSync(new URL('../popup.js', import.meta.url), 'utf8'));
    await settle();
    const get = id => dom.window.document.getElementById(id);
    assert.match(get('version').textContent, /수집기 시작 전/);
    assert.doesNotMatch(get('version').textContent, /새로고침 필요/);
    get('start').click(); await settle();
    assert.equal(get('resume').disabled, false);
    get('resume').click(); await settle();
    assert.equal(get('resume').disabled, false);
    get('resume').click(); await settle();
    assert.deepEqual(actions, ['start', 'resume', 'resume']);
    get('stop').click(); await settle();
    assert.equal(get('start').disabled, false);
    assert.equal(get('resume').disabled, false);
    dom.window.close();
});
