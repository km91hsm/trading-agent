import { apiCycle, abortApi } from './api-runner.js';
import { LANES, Scheduler, laneFor, marker, ownedLane, safePage, PdfPair } from './core.js';
const WORKER_VERSION = '1.2.0';
const scheduler = new Scheduler(), pdf = new PdfPair(), reportAttempts = new Map();
let limits = { max_scrolls: 2, max_details: 2, max_reports: 2 };
let port = null, requests = new Map(), seq = 0, enabled = false, token = '', ticking = null, retryAt = 0, failures = 0, planAt = 0, epoch = 0, nextTimer = null, presentRequested = false;
let apiSetupRequired = false;
const handoffReleases = new Map();
const PDF_BEGIN_ERRORS = { pdf_busy: 'pdf_begin_busy', unknown_report: 'pdf_begin_unknown_report', pdf_cached: 'pdf_begin_cached' };
const PDF_SAVE_ERRORS = { invalid_pdf_ticket: 'pdf_save_ticket', invalid_pdf_path: 'pdf_save_path', invalid_pdf: 'pdf_save_invalid' };
const PDF_CLICK_ERRORS = new Set(['hook_not_ready', 'button_unverified', 'candidate_missing', 'url_unverified']);
function nativePdfState(stage, error) { const allowed = stage === 'begin' ? PDF_BEGIN_ERRORS : PDF_SAVE_ERRORS; const code = error?.message; return Object.hasOwn(allowed, code) ? allowed[code] : stage === 'begin' ? 'pdf_begin_failed' : 'pdf_save_failed'; }
const pendingPaces = new Map();
const activeJobs = new Map();
function cancelPaces() {
    for (const [id, p] of pendingPaces) {
        clearTimeout(p.timer);
        pendingPaces.delete(id);
        try {
            p.reply({ ok: false, error: 'pace_stopped' });
        }
        catch { }
    }
}
function markerRepair(sender) {
    const proof = activeJobs.get(sender.tab?.id);
    if (!proof || proof.epoch !== epoch || proof.token !== token)
        return null;
    try {
        const u = new URL(sender.tab.url);
        if (u.hash !== '' || u.origin !== 'https://www.saveticker.com' || u.pathname !== '/news' || u.origin + u.pathname !== proof.page)
            return null;
        if (!((proof.stream === 'news_reports' && u.search === '?label_group=7') || (proof.stream === 'news_all' && u.search === '')))
            return null;
        u.hash = 'trader-saveticker=' + proof.token + ':' + proof.lane;
        return { url: u.href, proof };
    }
    catch {
        return null;
    }
}
function pace(message, sender, reply) {
    if (sender.id !== chrome.runtime.id || !enabled || message.ownerToken !== token || !Number.isInteger(sender.tab?.id)) {
        reply({ ok: false, error: 'pace_denied' });
        return;
    }
    const owned = ownedLane(sender.tab, token), repair = owned ? null : markerRepair(sender);
    if (!owned && !repair) {
        reply({ ok: false, error: 'pace_denied' });
        return;
    }
    const tabId = sender.tab.id;
    if (pendingPaces.has(tabId) || pendingPaces.size >= 4) {
        reply({ ok: false, error: 'pace_busy' });
        return;
    }
    const pending = { timer: null, reply };
    pendingPaces.set(tabId, pending);
    const deny = () => {
        if (pendingPaces.get(tabId) !== pending)
            return;
        pendingPaces.delete(tabId);
        reply({ ok: false, error: 'pace_denied' });
    };
    (async () => {
        if (repair) {
            await chrome.tabs.update(tabId, { url: repair.url });
            if (!enabled || activeJobs.get(tabId) !== repair.proof || repair.proof.epoch !== epoch) {
                deny();
                return;
            }
        }
        if (pendingPaces.get(tabId) !== pending)
            return;
        pending.timer = setTimeout(() => {
            if (pendingPaces.get(tabId) !== pending)
                return;
            pendingPaces.delete(tabId);
            reply(enabled ? { ok: true } : { ok: false, error: 'pace_stopped' });
        }, 2000);
    })().catch(deny);
}
function releaseHandoff(nonce) {
    const h = handoffReleases.get(nonce);
    if (!h)
        return;
    handoffReleases.delete(nonce);
    clearTimeout(h.timer);
    void chrome.tabs.sendMessage(h.tabId, { type: h.api ? 'api_pdf_release' : 'pdf_release', nonce, token: h.token }).catch(() => { });
}
function releaseAllHandoffs() {
    for (const nonce of [...handoffReleases.keys()])
        releaseHandoff(nonce);
}
const currentPdf = p => !!p && enabled && p.epoch === epoch && pdf.pending === p && !p.cancelled;
async function cancelOwnedDownload(id) {
    if (!Number.isInteger(id) || id < 0)
        return;
    try {
        await chrome.downloads.cancel(id);
    }
    catch { }
}
function discardPdf(p) {
    p?.resolve?.(false);
    if (pdf.pending === p)
        pdf.clear();
    if (p?.nonce)
        releaseHandoff(p.nonce);
}
function abortPdf(message, sender, reply) {
    const p = pdf.pending;
    let allowed = false;
    try {
        const proof = p?.proof;
        const handoff = handoffReleases.get(message.nonce);
        // Cancellation acts only on the already bound download. Navigation may
        // have changed the current route before MAIN's pagehide message arrives.
        allowed = sender.id === chrome.runtime.id && enabled && p?.nonce === message.nonce && p.ownerToken === message.ownerToken && message.ownerToken === token && p.tabId === sender.tab?.id && p.epoch === epoch && proof?.stream === 'news_reports' && proof.token === token && proof.epoch === epoch && handoff?.pending === p && handoff.proof === proof && handoff.tabId === p.tabId && handoff.token === token;
    }
    catch { }
    if (!allowed) {
        reply({ ok: false, error: 'pdf_abort_denied' });
        return;
    }
    p.cancelled = true;
    if (p.downloadId !== null) {
        void cancelOwnedDownload(p.downloadId);
        discardPdf(p);
    }
    else
        releaseHandoff(p.nonce);
    void store({ pdfState: 'handoff_aborted' });
    reply({ ok: true });
}
async function finishPdf(item, expected = pdf.pending) {
    if (!currentPdf(expected))
        return;
    const pending = expected, pair = pdf.complete(item || {});
    if (!pair) {
        if (item?.id === pending.downloadId && item.state === 'complete') {
            discardPdf(pending);
            await store({ pdfState: 'filename_unverified' });
        }
        return;
    }
    if (pair.nonce)
        releaseHandoff(pair.nonce);
    if (!enabled || !port || pending.cancelled || pending.epoch !== epoch)
        return;
    try {
        await request('pdf', { item_id: pair.item_id, ticket: pair.ticket, download_name: pair.download_name });
        if (enabled && pending.epoch === epoch && !pending.cancelled && !pdf.pending) {
            await store({ pdfState: 'saved' });
            pending.resolve?.(true);
        } else pending.resolve?.(false);
    }
    catch (error) {
        pending.resolve?.(false);
        if (enabled && pending.epoch === epoch && !pending.cancelled && !pdf.pending)
            await store({ pdfState: nativePdfState('save', error) });
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
async function loadPlan(plan) {
    apiSetupRequired = plan.api_setup_required === true;
    scheduler.plan(plan.views);
    const saved = await chrome.storage.local.get('collectionSchedule');
    scheduler.restore(saved.collectionSchedule);
    limits = plan.limits || limits;
    planAt = Date.now();
    await store({ collectionSchedule: scheduler.snapshot(), apiSupportedCount: plan.views.filter(v => v.api_supported === true).length, apiUnsupported: plan.views.filter(v => v.api_supported === false).map(v => v.stream) });
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
        cancelPaces();
        activeJobs.clear();
        if (pdf.pending) {
            pdf.pending.cancelled = true;
            pdf.pending.resolve?.(false);
        }
        pdf.clear();
        releaseAllHandoffs();
        for (const p of requests.values()) {
            clearTimeout(p.timer);
            p.reject(Error('disconnected'));
        }
        requests.clear();
        retryAt = Date.now() + Math.min(60000, 1000 * 2 ** failures++);
        void store({ connection: enabled ? '연결 끊김 · 다시 연결 대기' : '중지됨' });
    });
    const plan = await request('hello', { protocol: 1 });
    await loadPlan(plan);
    failures = 0;
    await store({ connection: '연결됨', planCount: plan.views.length, workerVersion: WORKER_VERSION });
}
async function pauseWindow() {
    const status = { connection: '전용 창이 최소화됨 · 창을 복원해 주세요' };
    for (const lane of LANES) {
        status['lane_error_' + lane] = 'window_minimized';
        status['lane_' + lane] = '전용 창 복원 대기';
        await request('state', { lane, state: 'paused', reason: 'page_not_loaded' });
    }
    await store(status);
}
async function tabs(runEpoch) {
    const active = () => enabled && runEpoch === epoch;
    let all = await chrome.tabs.query({ url: 'https://www.saveticker.com/*' });
    const marked = all.filter(t => ownedLane(t, token));
    const saved = (await chrome.storage.local.get('ownedWindowId')).ownedWindowId;
    let ownedWindow = null, createdWindow = false;
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
        createdWindow = true;
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
            if (view) {
                const requested = marker(view.url, token, lane);
                const created = await chrome.tabs.create({ windowId: ownedWindow.id, url: requested, active: false });
                result[lane] = { ...created, pendingUrl: created.pendingUrl || created.url || requested };
            }
        }
    }
    if (active() && (presentRequested || createdWindow)) {
        await chrome.windows.update(ownedWindow.id, { focused: true });
        presentRequested = false;
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
async function blockHost() {
    for (const lane of LANES)
        scheduler.block(lane);
    cancelPaces();
    if (pdf.pending) {
        pdf.pending.cancelled = true;
        pdf.pending.resolve?.(false);
        if (pdf.pending.downloadId !== null)
            void cancelOwnedDownload(pdf.pending.downloadId);
        pdf.clear();
    }
    releaseAllHandoffs();
    const status = { blockedLanes: [...LANES], hostBlocked: true, connection: '사용자 확인 필요 · 수집 일시 중지', lastAttempt: Date.now() };
    for (const lane of LANES) {
        status['lane_' + lane] = '사용자 확인 필요';
        status['lane_error_' + lane] = null;
    }
    await store(status);
    for (const lane of LANES) {
        try { await request('state', { lane, state: 'waiting_verification', reason: 'verification_required' }); }
        catch { }
    }
}
async function apiPdf(item, tab, runEpoch) {
    if (!enabled || runEpoch !== epoch || pdf.pending) return;
    const ticket = await request('api_pdf_begin', { item_id: item.id });
    if (!enabled || runEpoch !== epoch) return;
    const nonce = ticket.job_id;
    pdf.begin({ ...ticket, item_id: item.id, direct: true, nonce, tabId: tab.id, ownerToken: token, epoch: runEpoch, cancelled: false });
    const pending = pdf.pending;
    let timer = null;
    const completed = new Promise(resolve => { pending.resolve = resolve; });
    handoffReleases.set(nonce, { tabId: tab.id, token, pending, api: true, timer: null });
    try {
        await store({ pdfState: 'api_fetching' });
        await new Promise(resolve => setTimeout(resolve, 2000));
        if (!currentPdf(pending)) return;
        const captured = await command(tab.id, { type: 'api_fetch', token, job: ticket });
        if (!currentPdf(pending)) return;
        if ([401, 403, 429].includes(captured?.status)) { discardPdf(pending); return captured; }
        if (captured?.error || captured?.status !== 200 || captured.url !== ticket.request.url || captured.nonce !== nonce || captured.content_type !== 'application/pdf') throw Error('api_pdf_unverified');
        const url = new URL(captured.blob_url);
        if (url.protocol !== 'blob:' || url.origin !== 'https://www.saveticker.com') throw Error('api_pdf_unverified');
        pdf.expect(url.href);
        if (!currentPdf(pending)) return;
        const id = await chrome.downloads.download({ url: url.href, filename: 'SaveTicker/' + ticket.download_name, conflictAction: 'uniquify', saveAs: false });
        if (!currentPdf(pending)) { await cancelOwnedDownload(id); return; }
        pdf.bind(id);
        await store({ pdfState: 'downloading' });
        timer = setTimeout(() => {
            if (!currentPdf(pending)) return;
            pending.cancelled = true;
            void cancelOwnedDownload(pending.downloadId);
            discardPdf(pending);
        }, Math.max(1, Date.parse(ticket.expires_at) - Date.now() - 10000));
        timer?.unref?.();
        const [download] = await chrome.downloads.search({ id });
        if (download?.state === 'complete') await finishPdf(download, pending);
        if (!await completed && enabled && runEpoch === epoch) throw Error('api_pdf_failed');
    } catch {
        if (enabled && runEpoch === epoch) await store({ pdfState: 'api_failed' });
        throw Error('api_pdf_failed');
    } finally {
        clearTimeout(timer);
        if (currentPdf(pending)) discardPdf(pending);
        releaseHandoff(nonce);
    }
}
async function laneRun(lane, tab, runEpoch) {
    const view = scheduler.next(Date.now(), lane);
    if (!view)
        return;
    let job = null, consumed = false, failureCode = 'begin_failed', failed = false, proof = null;
    try {
        failureCode = 'ownership_unverified';
        if (ownedLane(tab, token) !== lane)
            throw Error('ownership_unverified');
        failureCode = 'begin_failed';
        job = await request('begin', { stream: view.stream });
        if (!enabled || runEpoch !== epoch)
            return;
        proof = { lane, token, epoch: runEpoch, page: safePage(job.view.url), stream: view.stream };
        activeJobs.set(tab.id, proof);
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
        const contentBlocked = capture.status === 'auth_required';
        if (contentBlocked)
            await blockHost();
        failureCode = 'snapshot_failed';
        const saved = await request('snapshot', { ...capture, stream: view.stream, job_id: job.job_id });
        consumed = true;
        failureCode = 'postprocess_failed';
        if (capture.status === 'auth_required' || saved.status === 'auth_required') {
            if (!contentBlocked)
                await blockHost();
            return;
        }
        if (pdf.pending && Date.now() - pdf.pending.at > 120000)
            pdf.clear();
        let pdfUnverified = false;
        if (view.stream === 'news_reports' && saved.report_items) {
            for (const item of saved.report_items.filter(x => !x.attachment_ok).sort((a, b) => (reportAttempts.get(a.id) || 0) - (reportAttempts.get(b.id) || 0)).slice(0, Math.min(2, limits.max_reports))) {
                if (pdf.pending)
                    break;
                reportAttempts.set(item.id, Date.now());
                let ticket;
                try {
                    ticket = await request('pdf_begin', { item_id: item.id });
                }
                catch (error) {
                    await store({ pdfState: nativePdfState('begin', error) });
                    throw Error('pdf_native_failed');
                }
                const nonce = crypto.randomUUID().replaceAll('-', '');
                pdf.begin({ ...ticket, item_id: item.id, direct: true, nonce, tabId: tab.id, ownerToken: token, epoch: runEpoch, proof, cancelled: false });
                const pending = pdf.pending;
                handoffReleases.set(nonce, { tabId: tab.id, token, proof, pending, timer: null });
                await store({ pdfState: 'waiting_hook' });
                const clicked = await command(tab.id, { type: 'pdf_click', title: item.title, token, nonce });
                if (!currentPdf(pending)) {
                    pdfUnverified = true;
                    discardPdf(pending);
                    break;
                }
                if (!clicked.ok || !clicked.observed_url || clicked.nonce !== nonce) {
                    pdfUnverified = true;
                    discardPdf(pending);
                    await store({ pdfState: PDF_CLICK_ERRORS.has(clicked.reason) ? clicked.reason : 'url_unverified' });
                    break;
                }
                try {
                    pdf.expect(clicked.observed_url);
                    await store({ pdfState: 'starting' });
                    if (!currentPdf(pending)) {
                        pdfUnverified = true;
                        discardPdf(pending);
                        break;
                    }
                    const downloadId = await chrome.downloads.download({ url: clicked.observed_url, filename: 'SaveTicker/' + ticket.download_name, saveAs: false, conflictAction: 'uniquify' });
                    if (!currentPdf(pending)) {
                        await cancelOwnedDownload(downloadId);
                        discardPdf(pending);
                        pdfUnverified = true;
                        break;
                    }
                    pdf.bind(downloadId);
                    await store({ pdfState: 'downloading' });
                    if (!currentPdf(pending)) {
                        await cancelOwnedDownload(downloadId);
                        discardPdf(pending);
                        pdfUnverified = true;
                        break;
                    }
                    const h = handoffReleases.get(nonce);
                    if (h)
                        h.timer = setTimeout(() => releaseHandoff(nonce), 5000);
                    const [actual] = await chrome.downloads.search({ id: downloadId });
                    if (!currentPdf(pending))
                        break;
                    if (actual?.state === 'complete')
                        await finishPdf(actual, pending);
                    else if (actual?.state === 'interrupted') {
                        discardPdf(pending);
                        await store({ pdfState: 'download_interrupted' });
                    }
                }
                catch {
                    pdfUnverified = true;
                    if (currentPdf(pending)) {
                        discardPdf(pending);
                        await store({ pdfState: 'download_start_failed' });
                    }
                    else
                        discardPdf(pending);
                }
                break;
            }
        }
        const status = saved.status === 'auth_required' || capture.status === 'auth_required' ? 'auth_required' : saved.status === 'error' || capture.status === 'error' ? 'error' : saved.status || capture.status;
        failed = status === 'error' || pdfUnverified;
        const update = { ['lane_' + lane]: pdfUnverified ? 'PDF 연결 미확인 · 일부 범위' : status === 'auth_required' ? '사용자 확인 필요' : status === 'error' ? '선택 확인 실패' : Number(saved.count) > 0 ? '수집됨 · 일부 범위' : '항목 미확인 · 일부 범위', lastAttempt: Date.now() };
        if (Number(saved.count) > 0 && ['ok', 'partial'].includes(status))
            update.lastCollected = Date.now();
        update['lane_error_' + lane] = status === 'error' ? 'selection_unverified' : null;
        update['lane_release_error_' + lane] = null;
        await store(update);
    }
    catch (error) {
        failed = true;
        if (view.stream === 'news_reports' && pdf.pending?.proof === proof) {
            const nonce = pdf.pending.nonce;
            pdf.clear();
            if (nonce)
                releaseHandoff(nonce);
            await store({ pdfState: 'url_unverified' });
        }
        const code = error?.message === 'lane_busy' ? 'lane_busy' : failureCode;
        if (job && !consumed && port && enabled && runEpoch === epoch) {
            try {
                await request('snapshot', { stream: view.stream, job_id: job.job_id, page_url: safePage(job.view.url), html: '', selection: { loaded: false }, status: 'error', reason: 'collection_error' });
            }
            catch {
                await store({ ['lane_release_error_' + lane]: 'job_release_failed' });
            }
        }
        if (scheduler.blocked.size === LANES.length)
            await store({ ['lane_' + lane]: '사용자 확인 필요', ['lane_error_' + lane]: null });
        else
            await store({ ['lane_' + lane]: '수집 오류 · 다음 주기 대기', ['lane_error_' + lane]: code });
    }
    finally {
        if (activeJobs.get(tab.id) === proof)
            activeJobs.delete(tab.id);
        scheduler.done(view, Date.now(), failed);
        await store({ collectionSchedule: scheduler.snapshot() });
    }
}
export async function tick() {
    if (ticking)
        return ticking;
    ticking = (async () => {
        const state = await chrome.storage.local.get(['enabled', 'ownerToken', 'blockedLanes', 'hostBlocked', 'backend', 'apiCooldownUntil', 'apiSetupRetryAt']);
        enabled = state.enabled === true;
        if (!enabled)
            return;
        if (state.hostBlocked || (state.blockedLanes || []).length) {
            for (const lane of LANES)
                scheduler.block(lane);
        }
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
                await loadPlan(plan);
            }
            if (scheduler.blocked.size === LANES.length) {
                // Keep Native diagnostics paused instead of resetting them with ping.
                for (const lane of LANES) {
                    if (!enabled || runEpoch !== epoch) return;
                    await request('state', { lane, state: 'waiting_verification', reason: 'verification_required' });
                }
                if (!enabled || runEpoch !== epoch) return;
                await store({ connection: '사용자 확인 필요 · 수집 일시 중지' });
                return;
            }
            await request('ping');
            if (!enabled || runEpoch !== epoch)
                return;
            if (Number.isFinite(state.apiCooldownUntil) && Date.now() < state.apiCooldownUntil) {
                await store({ connection: '요청 제한 · 서버 대기 시간 적용' });
                return;
            }
            if (state.backend === 'api') {
                if (apiSetupRequired && Number.isFinite(state.apiSetupRetryAt) && Date.now() < state.apiSetupRetryAt) return;
                await apiCycle({ chrome, token, scheduler, active: () => enabled && runEpoch === epoch, request, store, blockHost, setupRequired: apiSetupRequired, onSetup: async () => loadPlan(await request('hello', { protocol: 1 })), onPdf: (item, tab) => apiPdf(item, tab, runEpoch) });
                return;
            }
            const owned = await tabs(runEpoch);
            if (!owned)
                return;
            for (const lane of LANES) {
                if (!enabled || runEpoch !== epoch || scheduler.blocked.size === LANES.length)
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
    if (action === 'start' || action === 'resume')
        presentRequested = true;
    if (action === 'stop') {
        enabled = false;
        if ((await chrome.storage.local.get('backend')).backend === 'api')
            void abortApi(chrome, token).catch(() => {});
        cancelPaces();
        activeJobs.clear();
        epoch++;
        clearTimeout(nextTimer);
        nextTimer = null;
        await store({ enabled: false, connection: '중지됨' });
        await chrome.alarms.clear('saveticker');
        if (pdf.pending) {
            pdf.pending.cancelled = true;
            pdf.pending.resolve?.(false);
        }
        pdf.clear();
        releaseAllHandoffs();
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
        await store({ blockedLanes: [], hostBlocked: false });
    }
    await store({ enabled: true });
    enabled = true;
    retryAt = 0;
    await chrome.alarms.create('saveticker', { periodInMinutes: 0.5 });
    void tick();
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.type === 'pdf_abort') {
        abortPdf(message, sender, reply);
        return true;
    }
    if (message.type === 'pace') {
        pace(message, sender, reply);
        return true;
    }
    if (sender.id !== chrome.runtime.id || sender.tab)
        return;
    if (message.action === 'set_backend') {
        (async () => {
            if ((await chrome.storage.local.get('enabled')).enabled || !['api', 'dom'].includes(message.backend)) return { ok: false };
            scheduler.times.clear(); scheduler.retryAt.clear(); scheduler.failureStreak.clear();
            await store({ backend: message.backend, collectionSchedule: scheduler.snapshot() });
            return { ok: true };
        })().then(reply);
        return true;
    }
    if (['start', 'stop', 'resume'].includes(message.action)) {
        control(message.action).then(() => reply({ ok: true }));
        return true;
    }
});
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'saveticker')
        void tick();
});
const restore = async (present = false) => {
    if ((await chrome.storage.local.get('enabled')).enabled) {
        if (present)
            presentRequested = true;
        await chrome.alarms.create('saveticker', { periodInMinutes: 0.5 });
        void tick();
    }
};
chrome.runtime.onStartup.addListener(() => restore(true));
chrome.runtime.onInstalled.addListener(() => restore(false));
void restore();
// The download API return is the sole ID binding; no global filename observer.
chrome.downloads.onChanged.addListener(async (delta) => {
    const pending = pdf.pending;
    if (!currentPdf(pending) || delta.id !== pending.downloadId)
        return;
    if (delta.state?.current === 'interrupted') {
        discardPdf(pending);
        await store({ pdfState: 'download_interrupted' });
        return;
    }
    if (delta.state?.current !== 'complete')
        return;
    try {
        const [item] = await chrome.downloads.search({ id: delta.id });
        await finishPdf(item, pending);
    }
    catch {
        if (currentPdf(pending))
            await store({ pdfState: 'pdf_save_failed' });
    }
});
