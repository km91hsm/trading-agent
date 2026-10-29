import { LANES, Scheduler, laneFor, marker, ownedLane, safePage, PdfPair } from './core.js';
const WORKER_VERSION = '1.0.3';
const scheduler = new Scheduler(), pdf = new PdfPair(), reportAttempts = new Map();
let limits = { max_scrolls: 2, max_details: 2, max_reports: 2 };
let port = null, requests = new Map(), seq = 0, enabled = false, token = '', ticking = null, retryAt = 0, failures = 0, planAt = 0, epoch = 0, nextTimer = null;
const downloadWaiters = new Map();
function settleDownloads() {
    for (const [id, w] of downloadWaiters) {
        clearTimeout(w.timer);
        const name = pdf.claim(w.item);
        w.suggest(name ? { filename: name, conflictAction: 'uniquify' } : undefined);
        downloadWaiters.delete(id);
    }
}
function scheduleNext() {
    clearTimeout(nextTimer);
    nextTimer = null;
    if (enabled) {
        nextTimer = setTimeout(() => { nextTimer = null; void tick(); }, port ? 5000 : Math.max(1000, retryAt - Date.now()));
        nextTimer?.unref?.();
    }
}
const store = values => chrome.storage.local.set(values);
function request(type, body = {}) {
    if (!port)
        return Promise.reject(Error('disconnected'));
    const id = ++seq;
    return new Promise((resolve, reject) => { const timer = setTimeout(() => { requests.delete(id); reject(Error('host_timeout')); }, 20000); requests.set(id, { resolve, reject, timer }); port.postMessage({ type, id, ...body }); });
}
async function connect() {
    if (port)
        return;
    const activePort = chrome.runtime.connectNative('com.trader.saveticker');
    port = activePort;
    activePort.onMessage.addListener(msg => {
        if (port !== activePort)
            return;
        const p = requests.get(msg.id);
        if (p) {
            clearTimeout(p.timer);
            requests.delete(msg.id);
            msg.ok ? p.resolve(msg) : p.reject(Error(msg.error || 'host_error'));
        }
    });
    activePort.onDisconnect.addListener(() => {
        if (port !== activePort)
            return;
        port = null;
        pdf.clear();
        settleDownloads();
        for (const p of requests.values()) {
            clearTimeout(p.timer);
            p.reject(Error('disconnected'));
        }
        requests.clear();
        retryAt = Date.now() + Math.min(60000, 1000 * 2 ** failures++);
        void store({ connection: enabled ? '연결 끊김 · 다시 연결 대기' : '중지됨' });
    });
    const plan = await request('hello', { protocol: 1 });
    scheduler.plan(plan.views);
    limits = plan.limits || limits;
    planAt = Date.now();
    failures = 0;
    await store({ connection: '연결됨', planCount: plan.views.length, workerVersion: WORKER_VERSION });
}
async function pauseWindow() { const status = { connection: '전용 창이 최소화됨 · 창을 복원해 주세요' }; for (const lane of LANES) {
    status['lane_error_' + lane] = 'window_minimized';
    status['lane_' + lane] = '전용 창 복원 대기';
    await request('state', { lane, state: 'paused', reason: 'page_not_loaded' });
} await store(status); }
async function tabs(runEpoch) {
    const active = () => enabled && runEpoch === epoch;
    let all = await chrome.tabs.query({ url: 'https://www.saveticker.com/*' });
    const marked = all.filter(t => ownedLane(t, token));
    const saved = (await chrome.storage.local.get('ownedWindowId')).ownedWindowId;
    let ownedWindow = null;
    if (Number.isInteger(saved) && marked.some(t => t.windowId === saved)) {
        try {
            ownedWindow = await chrome.windows.get(saved);
            if (ownedWindow.state === 'minimized') {
                await pauseWindow();
                return null;
            }
            const occupants = await chrome.tabs.query({ windowId: saved });
            if (!occupants.length || occupants.some(t => !ownedLane(t, token)))
                ownedWindow = null;
        }
        catch {
            if (ownedWindow?.state === 'minimized')
                throw Error('window_minimized');
            ownedWindow = null;
        }
    }
    if (!active())
        return null;
    if (!ownedWindow) {
        if (marked.length)
            ownedWindow = await chrome.windows.create({ tabId: marked[0].id, focused: false });
        else {
            const first = scheduler.views[0];
            if (!first)
                return {};
            const url = marker(first.url, token, laneFor(first));
            ownedWindow = await chrome.windows.create({ url, focused: false });
            const initial = ownedWindow.tabs?.[0] || (await chrome.tabs.query({ windowId: ownedWindow.id })).find(t => t.windowId === ownedWindow.id);
            if (initial)
                all.push({ ...initial, pendingUrl: initial.pendingUrl || (!initial.url ? url : undefined), windowId: ownedWindow.id });
        }
        if (marked.length)
            marked[0].windowId = ownedWindow.id;
        await store({ ownedWindowId: ownedWindow.id });
    }
    if (ownedWindow.state === 'minimized') {
        await pauseWindow();
        return null;
    }
    const result = {};
    for (const lane of LANES) {
        if (!active())
            break;
        const owned = all.filter(t => ownedLane(t, token) === lane);
        if (owned.length) {
            const tab = owned[0];
            if (tab.windowId !== ownedWindow.id) {
                await chrome.tabs.move(tab.id, { windowId: ownedWindow.id, index: -1 });
                tab.windowId = ownedWindow.id;
            }
            result[lane] = tab;
            for (const extra of owned.slice(1))
                await chrome.tabs.remove(extra.id);
        }
        else {
            const view = scheduler.views.find(v => laneFor(v) === lane);
            if (view)
                result[lane] = await chrome.tabs.create({ windowId: ownedWindow.id, url: marker(view.url, token, lane), active: false });
        }
    }
    await store({ connection: '연결됨' });
    return result;
}
async function command(tabId, message) {
    for (let i = 0; i < 10; i++) {
        try {
            return await chrome.tabs.sendMessage(tabId, message);
        }
        catch {
            await new Promise(r => setTimeout(r, 500));
        }
    }
    throw Error('tab_unavailable');
}
async function laneRun(lane, tab, runEpoch) {
    const view = scheduler.next(Date.now(), lane);
    if (!view)
        return;
    let job = null, consumed = false, failureCode = 'begin_failed';
    try {
        job = await request('begin', { stream: view.stream });
        if (!enabled || runEpoch !== epoch)
            return;
        failureCode = 'state_failed';
        await request('state', { lane, state: 'collecting' });
        let currentPage = null;
        try {
            currentPage = safePage(tab.pendingUrl || tab.url);
        }
        catch { }
        failureCode = 'navigation_failed';
        if (currentPage !== safePage(job.view.url)) {
            await chrome.tabs.update(tab.id, { url: marker(job.view.url, token, lane) });
            await new Promise(r => setTimeout(r, 2000));
        }
        failureCode = 'tab_activation_failed';
        await chrome.tabs.update(tab.id, { active: true });
        await new Promise(r => setTimeout(r, 2000));
        failureCode = 'content_unavailable';
        const capture = await command(tab.id, { type: 'collect', job: { ...job, limits }, token });
        if (!enabled || runEpoch !== epoch)
            return;
        failureCode = 'snapshot_failed';
        const saved = await request('snapshot', { ...capture, stream: view.stream, job_id: job.job_id });
        consumed = true;
        failureCode = 'postprocess_failed';
        if (capture.status === 'auth_required' || saved.status === 'auth_required') {
            scheduler.block(lane);
            await store({ blockedLanes: [...scheduler.blocked] });
            await request('state', { lane, state: 'waiting_verification', reason: 'verification_required' });
        }
        if (pdf.pending && Date.now() - pdf.pending.at > 120000)
            pdf.clear();
        let pdfUnverified = false;
        if (view.stream === 'news_reports' && saved.report_items) {
            for (const item of saved.report_items.filter(x => !x.attachment_ok).sort((a, b) => (reportAttempts.get(a.id) || 0) - (reportAttempts.get(b.id) || 0)).slice(0, Math.min(2, limits.max_reports))) {
                if (pdf.pending)
                    break;
                reportAttempts.set(item.id, Date.now());
                const ticket = await request('pdf_begin', { item_id: item.id });
                pdf.begin({ ...ticket, item_id: item.id });
                const clicked = await command(tab.id, { type: 'pdf_click', title: item.title, token });
                if (!clicked.ok || !clicked.observed_url) {
                    pdfUnverified = true;
                    pdf.clear();
                    settleDownloads();
                }
                else {
                    pdf.expect(clicked.observed_url);
                    settleDownloads();
                    break;
                }
            }
        }
        const status = saved.status === 'auth_required' || capture.status === 'auth_required' ? 'auth_required' : saved.status === 'error' || capture.status === 'error' ? 'error' : saved.status || capture.status;
        const update = { ['lane_' + lane]: pdfUnverified ? 'PDF 연결 미확인 · 일부 범위' : status === 'auth_required' ? '사용자 확인 필요' : status === 'error' ? '선택 확인 실패' : Number(saved.count) > 0 ? '수집됨 · 일부 범위' : '항목 미확인 · 일부 범위', lastAttempt: Date.now() };
        if (Number(saved.count) > 0 && ['ok', 'partial'].includes(status))
            update.lastCollected = Date.now();
        update['lane_error_' + lane] = status === 'error' ? 'selection_unverified' : null;
        update['lane_release_error_' + lane] = null;
        await store(update);
    }
    catch (error) {
        const code = error?.message === 'lane_busy' ? 'lane_busy' : failureCode;
        if (job && !consumed && port && enabled && runEpoch === epoch) {
            try {
                await request('snapshot', { stream: view.stream, job_id: job.job_id, page_url: safePage(job.view.url), html: '', selection: { loaded: false }, status: 'error', reason: 'collection_error' });
            }
            catch {
                await store({ ['lane_release_error_' + lane]: 'job_release_failed' });
            }
        }
        await store({ ['lane_' + lane]: '수집 오류 · 다음 주기 대기', ['lane_error_' + lane]: code });
    }
    finally {
        scheduler.done(view, Date.now());
    }
}
export async function tick() {
    if (ticking)
        return ticking;
    ticking = (async () => {
        const state = await chrome.storage.local.get(['enabled', 'ownerToken', 'blockedLanes']);
        enabled = state.enabled === true;
        if (!enabled)
            return;
        for (const l of state.blockedLanes || [])
            scheduler.block(l);
        token = state.ownerToken;
        if (!token) {
            token = crypto.randomUUID().replaceAll('-', '');
            await store({ ownerToken: token });
        }
        if (Date.now() < retryAt)
            return;
        try {
            const runEpoch = epoch;
            await connect();
            if (!enabled || runEpoch !== epoch)
                return;
            if (Date.now() - planAt > 300000) {
                const plan = await request('hello', { protocol: 1 });
                scheduler.plan(plan.views);
                limits = plan.limits || limits;
                planAt = Date.now();
            }
            await request('ping');
            if (!enabled || runEpoch !== epoch)
                return;
            const owned = await tabs(runEpoch);
            if (!owned)
                return;
            for (const lane of LANES) {
                if (!enabled || runEpoch !== epoch)
                    break;
                if (owned[lane])
                    await laneRun(lane, owned[lane], runEpoch);
            }
        }
        catch {
            retryAt = Date.now() + Math.min(60000, 1000 * 2 ** failures++);
            if (enabled)
                await store({ connection: '호스트 연결 확인 필요' });
        }
    })().finally(() => { ticking = null; scheduleNext(); });
    return ticking;
}
async function control(action) {
    if (action === 'stop') {
        enabled = false;
        epoch++;
        clearTimeout(nextTimer);
        nextTimer = null;
        await store({ enabled: false, connection: '중지됨' });
        await chrome.alarms.clear('saveticker');
        pdf.clear();
        settleDownloads();
        const oldPort = port;
        port = null;
        for (const p of requests.values()) {
            clearTimeout(p.timer);
            p.reject(Error('stopped'));
        }
        requests.clear();
        oldPort?.disconnect();
        return;
    }
    if (action === 'resume') {
        scheduler.resume();
        await store({ blockedLanes: [] });
    }
    await store({ enabled: true });
    enabled = true;
    retryAt = 0;
    await chrome.alarms.create('saveticker', { periodInMinutes: 0.5 });
    void tick();
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (sender.id !== chrome.runtime.id || sender.tab)
        return;
    if (['start', 'stop', 'resume'].includes(message.action)) {
        control(message.action).then(() => reply({ ok: true }));
        return true;
    }
});
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'saveticker')
        void tick();
});
const restore = async () => {
    if ((await chrome.storage.local.get('enabled')).enabled) {
        await chrome.alarms.create('saveticker', { periodInMinutes: 0.5 });
        void tick();
    }
};
chrome.runtime.onStartup.addListener(restore);
chrome.runtime.onInstalled.addListener(restore);
void restore();
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
    if (!pdf.pending) {
        suggest();
        return;
    }
    const name = pdf.claim(item);
    if (name) {
        suggest({ filename: name, conflictAction: 'uniquify' });
        return;
    }
    if (pdf.pending.expectedUrl || downloadWaiters.size >= 8) {
        suggest();
        return;
    }
    const timer = setTimeout(() => { downloadWaiters.delete(item.id); suggest(); }, 6000);
    downloadWaiters.set(item.id, { item, suggest, timer });
    return true;
});
chrome.downloads.onChanged.addListener(async (delta) => {
    if (delta.state?.current === 'interrupted' && pdf.pending?.downloadId === delta.id) {
        pdf.clear();
        return;
    }
    if (delta.state?.current !== 'complete')
        return;
    const [item] = await chrome.downloads.search({ id: delta.id });
    const pair = pdf.complete(item || {});
    if (pair && enabled && port) {
        try {
            await request('pdf', { item_id: pair.item_id, ticket: pair.ticket, download_name: pair.download_name });
        }
        catch {
            await store({ lane_background: 'PDF 저장 확인 실패' });
        }
    }
});
