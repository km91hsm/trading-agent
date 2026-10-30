import { LANES } from './core.js';
const ROOT = 'https://www.saveticker.com/news';
function owned(tab, token) {
    try { const u = new URL(tab.pendingUrl || tab.url); return u.origin === 'https://www.saveticker.com' && u.pathname === '/news' && u.hash === '#trader-saveticker-api=' + token; } catch { return false; }
}
export function retryDeadline(value, now = Date.now()) {
    let delay = typeof value === 'string' && /^\d+$/.test(value.trim()) ? Number(value.trim()) * 1000 : Date.parse(value) - now;
    if (!Number.isFinite(delay) || delay < 0 || delay > 31536000000) delay = 60000;
    return now + Math.max(60000, delay);
}
export async function abortApi(chrome, token) {
    const tabs = await chrome.tabs.query({ url: 'https://www.saveticker.com/*' });
    for (const tab of tabs.filter(t => owned(t, token)))
        try { await chrome.tabs.sendMessage(tab.id, { type: 'api_abort', token }); } catch { }
}
export async function apiCycle({ chrome, token, scheduler, active, request, store, blockHost, wait = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
    if (!active()) return;
    const candidates = await chrome.tabs.query({ url: 'https://www.saveticker.com/*' });
    if (!active()) return;
    let tab = candidates.find(t => owned(t, token));
    if (!tab) {
        // A single owned tab, no focus changes. Minimized windows are valid.
        const window = await chrome.windows.create({ url: ROOT + '#trader-saveticker-api=' + token, focused: false });
        if (!active()) return;
        tab = window.tabs?.[0];
        await store({ apiWindowId: window.id, connection: 'API 전용 탭 준비 중 · 다음 주기 대기' });
        if (!tab) return;
        // Do not send a message or navigate repeatedly while the page loads.
        return;
    }
    for (const lane of LANES) {
        if (!active()) break;
        const view = scheduler.next(Date.now(), lane);
        if (!view) continue;
        let job, saved = false, failed = true, authBlocked = false;
        try {
            job = await request('api_begin', { stream: view.stream });
            if (!active()) return;
            await wait(2000);
            if (!active()) return;
            const response = await chrome.tabs.sendMessage(tab.id, { type: 'api_fetch', token, job });
            if (!active()) return;
            if (response?.status === 401 || response?.status === 403) {
                authBlocked = true;
                await blockHost();
                return;
            }
            if (response?.status === 429) {
                // Persist before job cleanup so stop/disconnect cannot lose it.
                await store({ apiCooldownUntil: retryDeadline(response.retry_after), connection: '요청 제한 · 서버 대기 시간 적용' });
                return;
            }
            if (!response || response.error) throw Error(response?.error || 'api_content_unavailable');
            const result = await request('api_snapshot', { stream: view.stream, job_id: job.job_id, response });
            saved = true;
            if (!active()) return;
            failed = false;
            await store({ ['lane_' + lane]: 'API 부분 수집 · ' + (result.count || 0) + '개', ['lane_error_' + lane]: null, ...(result.count > 0 ? { lastCollected: Date.now() } : {}), apiCoverage: '일부 계약 검증 중 · 본문·PDF·순위 조건 미완료' });
        } catch (error) {
            if (!active()) return;
            const unsupported = error?.message === 'api_contract_unverified';
            await store({ ['lane_' + lane]: unsupported ? 'API 요청 조건 미확인 · 대기' : 'API 수집 오류 · 다음 주기 대기', ['lane_error_' + lane]: unsupported ? 'api_contract_unverified' : 'api_request_failed' });
        } finally {
            // Cancel only this exact job. Errors do not replace the last good capture.
            if (job && !saved && active()) try { await request('api_cancel', { stream: view.stream, job_id: job.job_id, ...(authBlocked ? { reason: 'verification_required' } : {}) }); } catch { }
            if (active()) {
                scheduler.done(view, Date.now(), failed);
                await store({ collectionSchedule: scheduler.snapshot() });
            }
        }
    }
}
