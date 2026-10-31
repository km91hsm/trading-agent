export const LANES = ['news', 'community', 'market', 'background'];
export function safePage(value) {
    const u = new URL(value);
    if (u.origin !== 'https://www.saveticker.com' || !['/news', '/community', '/watchlist', '/calendar'].includes(u.pathname))
        throw Error('invalid_page');
    return u.origin + u.pathname;
}
export const laneFor = v => v.kind === 'calendar' || v.stream === 'news_reports' ? 'background' : v.kind;
export const marker = (url, token, lane) => safePage(url) + '#trader-saveticker=' + token + ':' + lane;
export function ownedLane(tab, token) {
    try {
        const u = new URL(tab.pendingUrl || tab.url);
        safePage(u.href);
        return LANES.find(l => u.hash === '#trader-saveticker=' + token + ':' + l) || null;
    }
    catch {
        return null;
    }
}
export class Scheduler {
    constructor() { this.views = []; this.times = new Map(); this.retryAt = new Map(); this.failureStreak = new Map(); this.blocked = new Set(); this.hot = 0; }
    plan(v) {
        this.views = v.filter(x => LANES.includes(laneFor(x)) && Number.isFinite(Number(x.interval_s)) && Number(x.interval_s) >= 30).slice(0, 128);
        const allowed = new Set(this.views.map(v => v.stream));
        for (const map of [this.times, this.retryAt, this.failureStreak])
            for (const key of map.keys())
                if (!allowed.has(key)) map.delete(key);
    }
    snapshot() {
        return { version: 1, entries: this.views.filter(v => this.times.has(v.stream)).map(v => ({
            stream: v.stream, at: this.times.get(v.stream),
            ...(this.retryAt.has(v.stream) ? { retry_at: this.retryAt.get(v.stream), failure_streak: this.failureStreak.get(v.stream) } : {})
        })) };
    }
    restore(saved, now = Date.now()) {
        if (saved?.version !== 1 || !Array.isArray(saved.entries) || !Number.isFinite(now)) return;
        const allowed = new Map(this.views.map(v => [v.stream, v])), seen = new Set();
        for (const row of saved.entries.slice(0, 128)) {
            const v = row && allowed.get(row.stream);
            if (!v || seen.has(row.stream) || !Number.isFinite(row.at) || row.at < 0 || row.at > now) continue;
            const retry = Object.hasOwn(row, 'retry_at'), streak = Object.hasOwn(row, 'failure_streak');
            const normal = Number(v.interval_s) * 1000, base = Math.max(normal, 60000), cap = Math.max(normal, 900000);
            if (retry !== streak || (retry && (!Number.isFinite(row.retry_at) || row.retry_at < row.at + base || row.retry_at > row.at + cap || row.retry_at > now + cap || !Number.isInteger(row.failure_streak) || row.failure_streak < 1 || row.failure_streak > 32))) continue;
            seen.add(row.stream);
            this.times.set(row.stream, row.at);
            if (retry) {
                this.retryAt.set(row.stream, row.retry_at);
                this.failureStreak.set(row.stream, row.failure_streak);
            } else {
                this.retryAt.delete(row.stream);
                this.failureStreak.delete(row.stream);
            }
        }
    }
    next(now, lane) {
        let due = this.views.filter(v => (!lane || laneFor(v) === lane) && !this.blocked.has(laneFor(v)) && (this.retryAt.has(v.stream) ? now >= this.retryAt.get(v.stream) : now - (this.times.get(v.stream) ?? -Infinity) >= v.interval_s * 1000));
        due.sort((a, b) => (this.times.get(a.stream) ?? -Infinity) - (this.times.get(b.stream) ?? -Infinity));
        if (this.hot >= 8) {
            const cold = due.find(v => v.interval_s >= 300);
            if (cold) {
                this.hot = 0;
                return cold;
            }
        }
        return due[0];
    }
    done(v, now, failed = false) {
        this.times.set(v.stream, now);
        if (failed) {
            const streak = Math.min(32, (this.failureStreak.get(v.stream) || 0) + 1);
            this.failureStreak.set(v.stream, streak);
            const normal = Number(v.interval_s) * 1000;
            const base = Math.max(normal, 60000), cap = Math.max(normal, 900000);
            this.retryAt.set(v.stream, now + Math.min(cap, base * 2 ** Math.min(32, streak - 1)));
        }
        else {
            this.retryAt.delete(v.stream);
            this.failureStreak.delete(v.stream);
        }
        this.hot = v.interval_s < 300 ? this.hot + 1 : 0;
    }
    block(l) { this.blocked.add(l); }
    resume() { this.blocked.clear(); }
}
const KEYS = new Set('id title source name channel created_at is_headline_only content translations ko_KR tickers symbol content_labels label_name label events event_date event_date_only rank nameKo labelKo price changePercent turnover ticker regular text children body data items results rows'.split(' '));
export function sanitizeTree(v, d = 0) {
    if (d > 8)
        return null;
    if (typeof v === 'string')
        return v.slice(0, 200000);
    if (v === null || typeof v === 'number' || typeof v === 'boolean')
        return v;
    if (Array.isArray(v))
        return v.slice(0, 500).map(x => sanitizeTree(x, d + 1));
    if (typeof v === 'object')
        return Object.fromEntries(Object.entries(v).filter(([k]) => KEYS.has(k)).map(([k, x]) => [k, sanitizeTree(x, d + 1)]));
    return null;
}
export class PdfPair {
    constructor() { this.pending = null; }
    begin(job, now = Date.now()) {
        if (this.pending)
            throw Error('pdf_busy');
        if (!/^[a-f0-9]{32}-\d{13}\.pdf$/.test(job.download_name))
            throw Error('pdf_name');
        this.pending = { ...job, at: now, downloadId: null };
    }
    expect(url) {
        const u = new URL(url);
        if (!this.pending || !((u.protocol === 'blob:' && u.origin === 'https://www.saveticker.com') || (u.protocol === 'https:' && u.origin === 'https://www.saveticker.com')))
            throw Error('pdf_url_unverified');
        this.pending.expectedUrl = url;
    }
    bind(id) { if (!this.pending?.direct || !Number.isInteger(id) || id < 0 || this.pending.downloadId !== null)
        throw Error('pdf_id'); this.pending.downloadId = id; }
    claim(item) {
        const p = this.pending;
        if (!p || p.direct || !p.expectedUrl || item.url !== p.expectedUrl || p.downloadId !== null)
            return null;
        let u;
        try {
            u = new URL(item.referrer);
        }
        catch {
            return null;
        }
        const at = Date.parse(item.startTime);
        if (u.origin !== 'https://www.saveticker.com' || u.pathname !== '/news' || at < p.at || at > p.at + 120000 || !(item.mime === 'application/pdf' || /\.pdf(?:$|\?)/i.test(item.url || '')))
            return null;
        p.downloadId = item.id;
        return 'SaveTicker/' + p.download_name;
    }
    complete(item) {
        const p = this.pending;
        if (!p || item.id !== p.downloadId || item.state !== 'complete' || !item.filename?.replaceAll('\\', '/').endsWith('/SaveTicker/' + p.download_name))
            return null;
        this.pending = null;
        return p;
    }
    clear() { this.pending = null; }
}
