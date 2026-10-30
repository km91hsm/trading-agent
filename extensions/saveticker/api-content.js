// Isolated browser-managed same-origin reads. No MAIN hooks or credential reads.
(() => {
    const ORIGIN = 'https://www.saveticker.com', MAX = 2097152;
    const KEYS = new Set('news_list reports total_count page page_size events rankings items id title source name channel created_at is_headline_only content translations translated ko_KR tickers symbol content_labels label_name event_date event_date_only rank nameKo labelKo price changePercent turnover ticker regular text children body'.split(' '));
    let pending = null;
    function project(value, depth = 0) {
        if (depth > 8) return null;
        if (typeof value === 'string') return value.slice(0, 200000);
        if (value === null || typeof value === 'boolean' || typeof value === 'number') return value;
        if (Array.isArray(value)) return value.slice(0, 501).map(v => project(v, depth + 1));
        if (typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([k]) => KEYS.has(k)).map(([k, v]) => [k, project(v, depth + 1)]));
        return null;
    }
    function allowed(r) {
        if (r?.method !== 'GET' || r.max_bytes !== MAX) return false;
        try {
            const u = new URL(r.url);
            if (u.origin !== ORIGIN || u.username || u.password || u.hash) return false;
            const fixed = {
                news: '/api/news/list?page=1&page_size=20&sort=created_at_desc&label_group=1&label_name=1',
                reports: '/api/report/list?page=1&page_size=20',
                top5: '/api/stocks/api/v1/community/feed/ticker-rankings?limit=5',
                index: '/api/stocks/api/v1/market-panel'
            };
            if (Object.hasOwn(fixed, r.kind)) return u.href === ORIGIN + fixed[r.kind];
            if (r.kind !== 'calendar' || u.pathname !== '/api/calendar/events' || !/^\?start_date=\d{4}-\d{2}-\d{2}&end_date=\d{4}-\d{2}-\d{2}$/.test(u.search)) return false;
            const start = Date.parse(u.searchParams.get('start_date')), end = Date.parse(u.searchParams.get('end_date'));
            return Number.isFinite(start) && Number.isFinite(end) && end > start && end - start <= 33 * 86400000;
        } catch { return false; }
    }
    async function live(token) {
        const s = await chrome.storage.local.get(['enabled', 'ownerToken', 'backend']);
        return s.enabled === true && s.backend === 'api' && s.ownerToken === token && location.hash === '#trader-saveticker-api=' + token;
    }
    function barrier() {
        const heading = document.querySelector('h1')?.textContent?.trim();
        return /^(잠시만 기다려 주세요|Just a moment)/i.test(document.title) || heading === '접속 환경을 확인하고 있습니다';
    }
    chrome.runtime.onMessage.addListener((message, sender, reply) => {
        if (sender.id !== chrome.runtime.id) return;
        if (message.type === 'api_abort') {
            if (message.token && location.hash === '#trader-saveticker-api=' + message.token) pending?.controller.abort();
            reply({ ok: true }); return;
        }
        if (message.type !== 'api_fetch') return;
        (async () => {
            const { token, job } = message;
            if (!/^[a-f0-9]{32}$/.test(token || '') || location.origin !== ORIGIN || location.hash !== '#trader-saveticker-api=' + token) return { error: 'api_denied' };
            if (!allowed(job?.request) || !/^[a-f0-9]{32}$/.test(job?.job_id || '') || !Number.isFinite(Date.parse(job.expires_at)) || Date.parse(job.expires_at) <= Date.now() || Date.parse(job.expires_at) > Date.now() + 120000) return { error: 'api_contract_unverified' };
            if (!await live(token)) return { error: 'api_stopped' };
            if (barrier()) return { status: 403 };
            if (pending) return { error: 'api_busy' };
            const own = { controller: new AbortController() }; pending = own;
            const timer = setTimeout(() => own.controller.abort(), Math.min(15000, Date.parse(job.expires_at) - Date.now()));
            try {
                const r = await fetch(job.request.url, { method: 'GET', credentials: 'same-origin', redirect: 'manual', cache: 'no-store', signal: own.controller.signal });
                if (!await live(token)) return { error: 'api_stopped' };
                if (r.type === 'opaqueredirect' || r.status >= 300 && r.status < 400 || r.url !== job.request.url || r.redirected) return { status: 403 };
                if ([401, 403, 429].includes(r.status)) return { status: r.status, retry_after: r.status === 429 ? r.headers.get('retry-after') : null };
                const type = (r.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
                if (type === 'text/html') return { status: 403 };
                if (r.status !== 200 || type !== 'application/json') return { error: 'api_response_unverified' };
                if (Number(r.headers.get('content-length')) > MAX) return { error: 'api_response_too_large' };
                const reader = r.body.getReader(), decoder = new TextDecoder(); let length = 0, text = '';
                try {
                    while (true) {
                        const chunk = await reader.read(); if (chunk.done) break;
                        length += chunk.value.byteLength;
                        if (length > MAX) { await reader.cancel(); return { error: 'api_response_too_large' }; }
                        text += decoder.decode(chunk.value, { stream: true });
                    }
                    text += decoder.decode();
                } finally { reader.releaseLock(); }
                if (!await live(token) || own.controller.signal.aborted) return { error: 'api_stopped' };
                const data = JSON.parse(text);
                if (!data || typeof data !== 'object' || Array.isArray(data)) return { error: 'api_shape_unverified' };
                return { url: r.url, status: r.status, content_type: type, data: project(data) };
            } finally { clearTimeout(timer); if (pending === own) pending = null; }
        })().then(reply).catch(() => reply({ error: 'api_request_failed' }));
        return true;
    });
})();
