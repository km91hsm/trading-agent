// Isolated-world DOM collector. No credentials, requests or cookie access.
(() => {
    let collectionToken = null;
    const pdfHandoffs = new Map(), PDF_CHANNEL = 'trader-saveticker-pdf';
    function releasePdf(nonce) {
        for (const b of document.querySelectorAll('button[data-trader-pdf-nonce]'))
            if (b.getAttribute('data-trader-pdf-nonce') === nonce)
                b.removeAttribute('data-trader-pdf-nonce');
        const listener = pdfHandoffs.get(nonce);
        if (listener) {
            window.removeEventListener('message', listener);
            pdfHandoffs.delete(nonce);
        }
        window.postMessage({ channel: PDF_CHANNEL, type: 'release', nonce }, location.origin);
    }
    const pause = async () => {
        if (typeof chrome.runtime.sendMessage !== 'function')
            return new Promise(r => setTimeout(r, 2000));
        const result = await chrome.runtime.sendMessage({ type: 'pace', ownerToken: collectionToken });
        if (result?.ok !== true)
            throw Error('pace_stopped');
    };
    let busy = false, detailCursor = 0;
    const text = n => (n?.textContent || '').trim();
    const label = n => n.getAttribute('aria-label') || text(n);
    const nodes = () => [...document.querySelectorAll('button,[role="tab"],[role="checkbox"],[role="switch"]')];
    const find = (name, role) => nodes().find(n => label(n) === name && (!role || n.getAttribute('role') === role));
    const categoryNodes = () => [...document.querySelectorAll('[aria-label="뉴스 카테고리"] button')];
    const category = name => categoryNodes().find(n => label(n) === name);
    const visible = (n, includeDisabled = false) => {
        if (!n || (!includeDisabled && n.disabled) || n.closest('[hidden],[aria-hidden="true"]'))
            return false;
        for (let p = n; p; p = p.parentElement) {
            const style = getComputedStyle(p);
            if (style.display === 'none' || style.visibility === 'hidden')
                return false;
        }
        return true;
    };
    function ranking(v) {
        const trigger = nodes().find(n => visible(n) && !n.closest('aside,nav,[role="complementary"]') && label(n) === v.tab);
        for (let n = trigger?.parentElement; n && n !== document.body; n = n.parentElement) {
            if (n.querySelector('[data-stretched-link-card],[data-clickable]') && [...n.querySelectorAll('button,[role="checkbox"]')].some(c => label(c) === '정규장 기준' && visible(c, true)))
                return n;
        }
        return trigger?.parentElement;
    }
    const rankingFind = (v, name) => [...(ranking(v)?.querySelectorAll('button,[role="checkbox"],[role="switch"]') || [])].find(n => label(n) === name && visible(n, name === '정규장 기준'));
    const active = n => ['true', 'page'].includes(n?.getAttribute('aria-current')) || n?.getAttribute('aria-selected') === 'true' || n?.getAttribute('aria-pressed') === 'true' || n?.getAttribute('data-state') === 'active' || n?.getAttribute('data-selected') === 'true';
    const COMMUNITY_EQUIVALENCE = '지금은 실시간과 정규장 기준이 같아요';
    const communityView = v => v.kind === 'community' || v.stream?.startsWith('community_');
    function rankingActive(v, node) {
        if (active(node))
            return true;
        if (!communityView(v) || !node || !['지수', '상승', '하락'].includes(label(node)))
            return false;
        const selected = n => ['bg-neutral-800', 'text-background-secondary', 'text-medium-bold'].every(c => n.classList.contains(c));
        const scope = ranking(v);
        for (let group = node.parentElement; group; group = group.parentElement) {
            const buttons = [...group.querySelectorAll('button')].filter(n => visible(n) && ['지수', '상승', '하락'].includes(label(n)));
            if (['지수', '상승', '하락'].every(t => buttons.some(n => label(n) === t)))
                return buttons.filter(selected).length === 1 && selected(node);
            if (group === scope)
                break;
        }
        return false;
    }
    const equivalent = (v, control) => communityView(v) && control?.disabled === true && control.getAttribute('title') === COMMUNITY_EQUIVALENCE && check(control) === true;
    const check = n => { const v = n?.getAttribute('aria-checked') ?? n?.getAttribute('aria-pressed'); return v === 'true' ? true : v === 'false' ? false : undefined; };
    async function click(n) {
        if (!n || n.disabled)
            throw Error('selection_unverified');
        const rect = n.getBoundingClientRect();
        const options = { bubbles: true, cancelable: true, composed: true, button: 0, buttons: 1, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
        const Pointer = window.PointerEvent || window.MouseEvent;
        n.dispatchEvent(new Pointer('pointerdown', { ...options, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
        n.dispatchEvent(new MouseEvent('mousedown', options));
        if (!n.disabled)
            n.focus({ preventScroll: true });
        n.dispatchEvent(new Pointer('pointerup', { ...options, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
        n.dispatchEvent(new MouseEvent('mouseup', { ...options, buttons: 0 }));
        n.click();
        await pause();
    }
    function barrier() {
        const challenges = new Set(['접속 환경을 확인하고 있습니다', 'Checking your connection', 'Just a moment']);
        if ([document.title.trim(), ...[...document.querySelectorAll('h1')].filter(n => visible(n, true)).map(text)].some(value => challenges.has(value)))
            return true;
        const s = text(document.body); return /사람인지 확인|보안 확인|Verify you are human|Access Denied|Too Many Requests|잠시 후 다시|로그인이 필요|이메일로 로그인/i.test(s); }
    function auth() { return barrier() || !find('프로필 메뉴'); }
    function month() { const previous = find('이전'); const m = text(previous?.parentElement).match(/(\d{4})년\s*(\d{1,2})월/); return m ? `${m[1]}-${m[2].padStart(2, '0')}` : undefined; }
    function clean(root) {
        const clone = root.cloneNode(true);
        clone.querySelectorAll('script,style,iframe,form,input,textarea,header,nav,menu,[role="menu"],[role="menubar"],svg,img,video,audio').forEach(n => n.remove());
        for (const el of [clone, ...clone.querySelectorAll('*')]) {
            for (const a of [...el.attributes]) {
                if (!['id', 'class', 'role', 'aria-label', 'aria-current', 'aria-selected', 'aria-checked', 'aria-pressed', 'data-state', 'data-selected', 'data-read', 'data-clickable', 'data-stretched-link-card', 'datetime'].includes(a.name))
                    el.removeAttribute(a.name);
            }
        }
        return clone.outerHTML;
    }
    const byteLength = value => new TextEncoder().encode(value).length;
    function boundedNodes(nodes, start, end, limit, max = 100) {
        let result = start, count = 0, size = byteLength(start + end);
        for (const node of nodes) {
            if (count >= max)
                break;
            if (!visible(node, true))
                continue;
            const value = clean(node), n = byteLength(value);
            if (n > limit - size)
                continue;
            result += value;
            size += n;
            count++;
        }
        return result + end;
    }
    function calendarData(v) { const wanted = Number(v.month.slice(5, 7)); const headers = [...document.querySelectorAll('h3')].filter(n => visible(n, true) && /^(\d{1,2})월\s+\d{1,2}일\s+.+요일$/.test(text(n))); const matching = headers.filter(n => Number(text(n).match(/^(\d+)/)[1]) === wanted); const empty = [...document.querySelectorAll('p,span,div')].some(n => visible(n, true) && text(n) === '이 달에 일정이 없어요'); return { ready: headers.length > 0 ? matching.length === headers.length : empty, groups: [...new Set(matching.map(n => n.parentElement))], empty: headers.length === 0 && empty }; }
    function frame(v) {
        if (v.stream === 'news_all')
            return boundedNodes([...document.querySelectorAll('#news-tabpanel [data-read][role="button"][aria-label]')], '<section id="news-tabpanel">', '</section>', 512 * 1024, 100);
        if (v.stream === 'news_reports') {
            const groups = [];
            for (const button of document.querySelectorAll('button[aria-label="PDF 다운로드"]')) {
                if (!visible(button, true))
                    continue;
                for (let n = button.parentElement; n && n !== document.body; n = n.parentElement) {
                    if (n.querySelectorAll('button[aria-label="PDF 다운로드"]').length === 1 && /^\d{4}년 \d{1,2}월 \d{1,2}일 \(.+\)$/.test(text(n.querySelector('p')))) {
                        groups.push(n);
                        break;
                    }
                }
            }
            return boundedNodes([...new Set(groups)], '<section>', '</section>', 512 * 1024, 100);
        }
        if (v.month) {
            const data = calendarData(v);
            if (data.empty)
                return '<section><p>이 달에 일정이 없어요</p></section>';
            return boundedNodes(data.groups, '<section>', '</section>', 1024 * 1024, 500);
        }
        return clean(document.body);
    }
    function selection(v) {
        const result = { loaded: document.readyState === 'complete' && !auth(), visible: document.visibilityState === 'visible' };
        if (v.source)
            result.source = nodes().filter(n => n.getAttribute('role') === 'tab' && active(n)).map(label).find(x => x === v.source);
        if (v.category)
            result.category = categoryNodes().filter(active).map(label).find(x => x === v.category);
        if (v.tab) {
            result.tab = rankingActive(v, rankingFind(v, v.tab)) ? v.tab : undefined;
            const control = rankingFind(v, '정규장 기준');
            result.regular = check(control);
            result.regular_disabled = control?.disabled === true;
            if (equivalent(v, control)) {
                result.regular_equivalent = true;
                result.regular_equivalence_label = COMMUNITY_EQUIVALENCE;
            }
        }
        if (v.month)
            result.month = month();
        return result;
    }
    function verified(v, s) { return s.loaded && ['source', 'category', 'tab', 'month'].every(k => !v[k] || s[k] === v[k]) && (!v.tab || s.regular === v.regular || (communityView(v) && s.regular === true && s.regular_disabled === true && s.regular_equivalent === true && s.regular_equivalence_label === COMMUNITY_EQUIVALENCE)); }
    async function waitControl(predicate, budget, maxPauses = 7) {
        for (let i = 0; !predicate(); i++) {
            if (auth())
                throw Error('auth_required');
            if (i >= maxPauses || budget.readiness <= 0 || Date.now() >= budget.deadline)
                throw Error('selection_unverified');
            budget.readiness--;
            await pause();
        }
        if (auth())
            throw Error('auth_required');
    }
    async function select(v, budget) {
        const step = async (node) => {
            if (auth())
                throw Error('auth_required');
            if (Date.now() >= budget.deadline)
                throw Error('selection_unverified');
            await click(node);
        };
        if (v.source) {
            await waitControl(() => visible(find(v.source, 'tab')), budget);
            if (!active(find(v.source, 'tab')))
                await step(find(v.source, 'tab'));
        }
        if (v.category) {
            await waitControl(() => visible(category(v.category)), budget);
            if (!active(category(v.category)))
                await step(category(v.category));
        }
        if (v.tab) {
            await waitControl(() => rankingFind(v, v.tab), budget);
            if (!rankingActive(v, rankingFind(v, v.tab)))
                await step(rankingFind(v, v.tab));
            await waitControl(() => rankingFind(v, '정규장 기준'), budget);
            const control = rankingFind(v, '정규장 기준');
            if (check(control) !== v.regular && !equivalent(v, control))
                await step(control);
            const risk = rankingFind(v, '위험주 제외');
            if (check(risk) === true)
                await step(risk);
        }
        if (v.month) {
            await waitControl(() => visible(find('월별')), budget);
            if (!active(find('월별')))
                await step(find('월별'));
            await waitControl(() => month() && find('이전') && find('다음'), budget, 2);
            while (month() !== v.month) {
                if (budget.navigation <= 0)
                    throw Error('selection_unverified');
                const current = month();
                if (!current)
                    throw Error('selection_unverified');
                budget.navigation--;
                await step(find(current < v.month ? '다음' : '이전'));
            }
            const all = nodes().find(n => label(n) === '전체' && (n.hasAttribute('aria-pressed') || n.hasAttribute('aria-checked')) && visible(n));
            if (check(all) === false)
                await step(all);
            if (all && check(all) !== true)
                throw Error('selection_unverified');
        }
    }
    async function freshNews() {
        if (auth())
            throw Error('auth_required');
        let panel = document.querySelector('#news-tabpanel');
        const toolbar = document.querySelector('[aria-label="뉴스 목록 필터"]');
        let scope = toolbar;
        if (panel && toolbar)
            for (let n = panel.parentElement; n && n !== document.body; n = n.parentElement)
                if (n.contains(toolbar)) {
                    scope = n;
                    break;
                }
        const fresh = [...(scope?.querySelectorAll('button') || [])].find(n => label(n) === '새 뉴스' && visible(n) && !n.closest('aside,nav,[role="complementary"]'));
        if (fresh)
            await click(fresh);
        if (auth())
            throw Error('auth_required');
        panel = document.querySelector('#news-tabpanel');
        for (let n = panel; n; n = n.parentElement) {
            if (n === panel || n.scrollHeight > n.clientHeight)
                n.scrollTop = 0;
        }
        if (document.scrollingElement)
            document.scrollingElement.scrollTop = 0;
        await pause();
        if (auth())
            throw Error('auth_required');
    }
    async function scroll() {
        const candidates = [...document.querySelectorAll('*')].filter(n => n.clientHeight > 100 && n.scrollHeight > n.clientHeight + 30 && ['auto', 'scroll'].includes(getComputedStyle(n).overflowY));
        const panel = document.querySelector('#news-tabpanel');
        let n = panel;
        while (n && !(n.clientHeight > 100 && n.scrollHeight > n.clientHeight + 20))
            n = n.parentElement;
        n = n || candidates.sort((a, b) => b.clientHeight - a.clientHeight)[0];
        if (n)
            n.scrollBy(0, n.clientHeight * .8);
        else
            window.scrollBy(0, window.innerHeight * .7);
        await pause();
    }
    async function collect(job) {
        const limits = job.limits || { max_scrolls: 2, max_details: 2 };
        const v = job.view, page_url = location.origin + location.pathname;
        let html = '', details = [], s = selection(v);
        const budget = { readiness: 7, navigation: 26, deadline: Math.min(Date.now() + 90000, (Date.parse(job.expires_at) || Date.now() + 120000) - 20000) };
        try {
            for (let i = 0; i < 7 && !find('프로필 메뉴') && !barrier(); i++)
                await pause();
            if (auth())
                return { page_url, html: '', selection: { loaded: false, visible: document.visibilityState === 'visible' }, status: 'auth_required', reason: 'verification_required' };
            for (let attempt = 0; attempt < 2; attempt++) {
                try {
                    await select(v, budget);
                }
                catch (error) {
                    if (auth() || error?.message === 'pace_stopped')
                        throw error;
                    if (attempt === 1 || Date.now() >= budget.deadline)
                        throw error;
                    continue;
                }
                s = selection(v);
                if (verified(v, s))
                    break;
            }
            s = selection(v);
            if (!verified(v, s))
                return { page_url, html: '', selection: s, status: 'error', reason: 'selection_unverified' };
            if (v.stream === 'news_all') {
                await freshNews();
                s = selection(v);
                if (!verified(v, s))
                    throw Error('selection_unverified');
            }
            if (v.month) {
                try {
                    await waitControl(() => calendarData(v).ready, budget);
                }
                catch (error) {
                    if (auth() || error?.message === 'pace_stopped')
                        throw error;
                    throw Error('calendar_rows_unverified');
                }
            }
            const frames = [frame(v)];
            for (let i = 0; !v.month && i < Math.min(2, limits.max_scrolls); i++) {
                await scroll();
                if (auth())
                    throw Error('auth_required');
                if (!verified(v, selection(v)))
                    throw Error('selection_unverified');
                frames.push(frame(v));
            }
            html = frames.join('\n');
            if (v.stream === 'news_all') {
                const cards = [...document.querySelectorAll('#news-tabpanel [data-read][role="button"][aria-label]')];
                for (let i = 0; i < Math.min(2, limits.max_details, cards.length); i++) {
                    const card = cards[(detailCursor++) % cards.length], title = label(card);
                    await click(card);
                    if (auth())
                        throw Error('auth_required');
                    const heading = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')].find(n => text(n) === title);
                    if (heading) {
                        let panel = heading.closest('[role="dialog"]');
                        if (!panel)
                            for (let n = heading.parentElement; n && n !== document.body; n = n.parentElement)
                                if (n.querySelector('div.gap-6.p-6')) {
                                    panel = n;
                                    break;
                                }
                        if (panel) {
                            const detailHtml = clean(panel);
                            if (byteLength(detailHtml) <= 256 * 1024)
                                details.push({ title, html: detailHtml });
                        }
                    }
                    const close = find('닫기') || find('Close');
                    if (close)
                        await click(close);
                    else {
                        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                        document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', code: 'Escape', bubbles: true }));
                        await pause();
                    }
                    if (!document.querySelector('#news-tabpanel') || [...document.querySelectorAll('h2')].some(n => text(n) === title && n.closest('[role="dialog"]') || text(n) === title && n.parentElement?.querySelector('div.gap-6.p-6')))
                        throw Error('detail_close_unverified');
                }
            }
            s = selection(v);
            if (auth())
                throw Error('auth_required');
            if (!verified(v, s))
                throw Error('selection_unverified');
            if (new TextEncoder().encode(html + details.map(x => x.html).join('')).length > 4 * 1024 * 1024)
                return { page_url, html: '', selection: s, status: 'error', reason: 'snapshot_too_large' };
            return { page_url, html, details, selection: s, tab: v.tab, regular: v.regular, month: v.month, status: 'partial', reason: v.month ? (calendarData(v).empty ? 'empty' : 'partial') : 'scroll_limit' };
        }
        catch (e) {
            s = selection(v);
            return { page_url, html: '', selection: s, status: auth() ? 'auth_required' : 'error', reason: auth() ? 'verification_required' : e?.message === 'calendar_rows_unverified' ? 'calendar_rows_unverified' : 'selection_unverified' };
        }
    }
    chrome.runtime.onMessage.addListener((message, sender, reply) => {
        if (sender.id !== chrome.runtime.id)
            return;
        const token = message.token;
        if (!token || !['news', 'community', 'market', 'background'].some(l => location.hash === '#trader-saveticker=' + token + ':' + l)) {
            reply({ ok: false });
            return;
        }
        collectionToken = token;
        if (message.type === 'collect') {
            if (busy) {
                reply({ page_url: location.origin + location.pathname, html: '', selection: { loaded: false }, status: 'error', reason: 'lane_busy' });
                return;
            }
            busy = true;
            collect(message.job).then(reply).finally(() => { busy = false; });
            return true;
        }
        if (message.type === 'pdf_release') {
            if (/^[a-f0-9]{32}$/.test(message.nonce || ''))
                releasePdf(message.nonce);
            reply({ ok: true });
            return;
        }
        if (message.type === 'pdf_click') {
            const nonce = message.nonce;
            if (!/^[a-f0-9]{32}$/.test(nonce || '')) {
                reply({ ok: false });
                return;
            }
            const buttons = [...document.querySelectorAll('button[aria-label="PDF 다운로드"]')].filter(button => visible(button));
            const matches = buttons.filter(button => {
                for (let n = button.parentElement, i = 0; n && i < 7; n = n.parentElement, i++)
                    if (buttons.filter(candidate => n.contains(candidate)).length === 1 && text(n).includes(message.title))
                        return true;
                return false;
            });
            const match = matches.length === 1 ? matches[0] : null;
            if (!match || auth()) {
                reply({ ok: false, reason: 'button_unverified' });
                return;
            }
            let ready = false, aborted = false;
            const candidates = [];
            const listener = event => {
                if (event.source !== window || event.origin !== location.origin || event.data?.channel !== PDF_CHANNEL || event.data.nonce !== nonce)
                    return;
                const data = event.data;
                if (data.type === 'ready')
                    ready = true;
                if (data.type === 'aborted') {
                    aborted = true;
                    if (typeof chrome.runtime.sendMessage === 'function')
                        void Promise.resolve(chrome.runtime.sendMessage({ type: 'pdf_abort', nonce, ownerToken: collectionToken })).catch(() => { });
                }
                if (data.type === 'candidate') {
                    try {
                        if (typeof data.url !== 'string' || data.url.length > 2048)
                            throw Error('pdf_unverified');
                        const u = new URL(data.url);
                        if (u.protocol === 'blob:' && u.origin === location.origin)
                            candidates.push(u.href);
                        else
                            aborted = true;
                    }
                    catch {
                        aborted = true;
                    }
                }
            };
            match.setAttribute('data-trader-pdf-nonce', nonce);
            pdfHandoffs.set(nonce, listener);
            window.addEventListener('message', listener);
            window.postMessage({ channel: PDF_CHANNEL, type: 'arm', nonce }, location.origin);
            (async () => {
                let reason = 'hook_not_ready';
                try {
                    await pause();
                    if (!ready || aborted || auth())
                        throw Error('pdf_unverified');
                    reason = 'url_unverified';
                    match.click();
                    await pause();
                    await pause();
                    if (aborted || candidates.length !== 1) {
                        if (!aborted && candidates.length === 0)
                            reason = 'candidate_missing';
                        throw Error('pdf_unverified');
                    }
                    reply({ ok: true, observed_url: candidates[0], nonce });
                }
                catch {
                    releasePdf(nonce);
                    reply({ ok: false, reason });
                }
            })();
            return true;
        }
    });
})();
