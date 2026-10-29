// Isolated-world DOM collector. No credentials, requests or cookie access.
(() => {
    const pause = () => new Promise(r => setTimeout(r, 2000));
    let busy = false, detailCursor = 0;
    const text = n => (n?.textContent || '').trim();
    const label = n => n.getAttribute('aria-label') || text(n);
    const nodes = () => [...document.querySelectorAll('button,[role="tab"],[role="checkbox"],[role="switch"]')];
    const find = (name, role) => nodes().find(n => label(n) === name && (!role || n.getAttribute('role') === role));
    const categoryNodes = () => [...document.querySelectorAll('[aria-label="뉴스 카테고리"] button')];
    const category = name => categoryNodes().find(n => label(n) === name);
    const visible = n => { if (!n || n.disabled || n.closest('[hidden],[aria-hidden="true"]'))
        return false; for (let p = n; p; p = p.parentElement) {
        const style = getComputedStyle(p);
        if (style.display === 'none' || style.visibility === 'hidden')
            return false;
    } return true; };
    function ranking(v) { const trigger = nodes().find(n => visible(n) && label(n) === v.tab); for (let n = trigger?.parentElement; n && n !== document.body; n = n.parentElement) {
        if (n.querySelector('[data-stretched-link-card],[data-clickable]') && [...n.querySelectorAll('button,[role="checkbox"]')].some(c => label(c) === '정규장 기준' && visible(c)))
            return n;
    } return trigger?.parentElement; }
    const rankingFind = (v, name) => [...(ranking(v)?.querySelectorAll('button,[role="checkbox"],[role="switch"]') || [])].find(n => label(n) === name && visible(n));
    const active = n => ['true', 'page'].includes(n?.getAttribute('aria-current')) || n?.getAttribute('aria-selected') === 'true' || n?.getAttribute('aria-pressed') === 'true' || n?.getAttribute('data-state') === 'active' || n?.getAttribute('data-selected') === 'true';
    const check = n => { const v = n?.getAttribute('aria-checked') ?? n?.getAttribute('aria-pressed'); return v === 'true' ? true : v === 'false' ? false : undefined; };
    async function click(n) {
        if (!n || n.disabled)
            throw Error('selection_unverified');
        n.click();
        await pause();
    }
    function barrier() { const s = text(document.body); return /사람인지 확인|보안 확인|Verify you are human|Access Denied|Too Many Requests|잠시 후 다시|로그인이 필요|이메일로 로그인/i.test(s); }
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
    function selection(v) {
        const result = { loaded: document.readyState === 'complete' && !auth() };
        if (v.source)
            result.source = nodes().filter(n => n.getAttribute('role') === 'tab' && active(n)).map(label).find(x => x === v.source);
        if (v.category)
            result.category = categoryNodes().filter(active).map(label).find(x => x === v.category);
        if (v.tab) {
            result.tab = active(rankingFind(v, v.tab)) ? v.tab : undefined;
            result.regular = check(rankingFind(v, '정규장 기준'));
        }
        if (v.month)
            result.month = month();
        return result;
    }
    function verified(v, s) { return s.loaded && ['source', 'category', 'tab', 'month'].every(k => !v[k] || s[k] === v[k]) && (!v.tab || s.regular === v.regular); }
    async function select(v) {
        if (v.source && !active(find(v.source, 'tab')))
            await click(find(v.source, 'tab'));
        if (v.category && !active(category(v.category)))
            await click(category(v.category));
        if (v.tab) {
            if (!active(rankingFind(v, v.tab)))
                await click(rankingFind(v, v.tab));
            const control = rankingFind(v, '정규장 기준');
            if (check(control) !== v.regular)
                await click(control);
            const risk = rankingFind(v, '위험주 제외');
            if (check(risk) === true)
                await click(risk);
        }
        if (v.month) {
            await click(find('월별'));
            for (let i = 0; i < 26 && month() !== v.month; i++) {
                const current = month();
                if (!current)
                    throw Error('selection_unverified');
                await click(find(current < v.month ? '다음' : '이전'));
            }
            const all = nodes().find(n => label(n) === '전체' && (n.hasAttribute('aria-pressed') || n.hasAttribute('aria-checked')) && visible(n));
            if (check(all) === false)
                await click(all);
            if (all && check(all) !== true)
                throw Error('selection_unverified');
        }
    }
    async function freshNews() {
        if(auth()) throw Error('auth_required');
        let panel=document.querySelector('#news-tabpanel');
        const toolbar=document.querySelector('[aria-label="뉴스 목록 필터"]');
        let scope=toolbar;
        if(panel && toolbar) for(let n=panel.parentElement;n && n!==document.body;n=n.parentElement) if(n.contains(toolbar)){scope=n;break;}
        const fresh=[...(scope?.querySelectorAll('button')||[])].find(n=>label(n)==='새 뉴스' && visible(n) && !n.closest('aside,nav,[role="complementary"]'));
        if(fresh) await click(fresh);
        if(auth()) throw Error('auth_required');
        panel=document.querySelector('#news-tabpanel');
        for(let n=panel;n;n=n.parentElement) {if(n===panel || n.scrollHeight>n.clientHeight) n.scrollTop=0;}
        if(document.scrollingElement) document.scrollingElement.scrollTop=0;
        await pause();
        if(auth()) throw Error('auth_required');
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
        for (let i = 0; i < 7 && !find('프로필 메뉴') && !barrier(); i++)
            await pause();
        if (auth())
            return { page_url, html: '', selection: { loaded: false }, status: 'auth_required', reason: 'verification_required' };
        try {
            await select(v);
            s = selection(v);
            if (!verified(v, s))
                return { page_url, html: '', selection: s, status: 'error', reason: 'selection_unverified' };
            if(v.stream==='news_all') {await freshNews();s=selection(v);if(!verified(v,s))throw Error('selection_unverified');}
            const frames = [clean(document.body)];
            for (let i = 0; i < Math.min(2, limits.max_scrolls); i++) {
                await scroll();
                if (auth())
                    throw Error('auth_required');
                if (!verified(v, selection(v)))
                    throw Error('selection_unverified');
                frames.push(clean(document.body));
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
                        if (panel)
                            details.push({ title, html: clean(panel) });
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
            return { page_url, html, details, selection: s, tab: v.tab, regular: v.regular, month: v.month, status: 'partial', reason: 'scroll_limit' };
        }
        catch (e) {
            return { page_url, html: '', selection: s, status: auth() ? 'auth_required' : 'error', reason: auth() ? 'verification_required' : 'selection_unverified' };
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
        if (message.type === 'collect') {
            if (busy) {
                reply({ page_url: location.origin + location.pathname, html: '', selection: { loaded: false }, status: 'error', reason: 'lane_busy' });
                return;
            }
            busy = true;
            collect(message.job).then(reply).finally(() => { busy = false; });
            return true;
        }
        if (message.type === 'pdf_click') {
            const buttons = [...document.querySelectorAll('button[aria-label="PDF 다운로드"]')];
            const match = buttons.find(button => {
                let n = button.parentElement;
                for (let i = 0; n && i < 7; i++, n = n.parentElement) {
                    if (n.querySelectorAll('button[aria-label="PDF 다운로드"]').length === 1 && text(n).includes(message.title))
                        return true;
                }
                return false;
            });
            if (match && !auth()) {
                // Only a programmatic anchor click produced in this owned tab can identify
                // the button's download. An opaque direct download remains unverified.
                let finished = false;
                const urls = new Set();
                const observe = e => { const a = e.target.closest?.('a[href]'); if (a && !e.isTrusted) {
                    try {
                        const u = new URL(a.href);
                        if ((u.protocol === 'blob:' && u.origin === location.origin) || (u.protocol === 'https:' && u.origin === location.origin))
                            urls.add(u.href);
                    }
                    catch { }
                } };
                document.addEventListener('click', observe, true);
                match.click();
                setTimeout(() => { if (finished)
                    return; finished = true; document.removeEventListener('click', observe, true); reply(urls.size === 1 ? { ok: true, observed_url: [...urls][0] } : { ok: false, reason: 'pdf_unverified' }); }, 4000);
                return true;
            }
            else
                reply({ ok: false });
        }
    });
})();
