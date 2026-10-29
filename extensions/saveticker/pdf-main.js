// Static MAIN-world DOM handoff. Never observes requests, credentials or app state.
(() => {
    const CHANNEL = 'trader-saveticker-pdf', ORIGIN = 'https://www.saveticker.com';
    let armed = null;
    const emit = (type, nonce, extra = {}) => window.postMessage({ channel: CHANNEL, type, nonce, ...extra }, ORIGIN);
    function finish(reason) {
        const s = armed;
        if (!s)
            return;
        armed = null;
        clearTimeout(s.timer);
        if (HTMLAnchorElement.prototype.click === s.click)
            HTMLAnchorElement.prototype.click = s.originalClick;
        if (URL.revokeObjectURL === s.revoke)
            URL.revokeObjectURL = s.originalRevoke;
        for (const name of ['pointerdown', 'mousedown', 'click', 'keydown', 'touchstart', 'wheel'])
            document.removeEventListener(name, s.interaction, true);
        for (const name of ['pagehide', 'popstate', 'hashchange'])
            window.removeEventListener(name, s.navigation, true);
        if (s.deferred && s.url)
            s.originalRevoke.call(URL, s.url);
        if (reason)
            emit('aborted', s.nonce, { reason });
    }
    function current() {
        if (armed && (Date.now() >= armed.deadline || location.href !== armed.page)) {
            finish(Date.now() >= armed.deadline ? 'expired' : 'navigation');
            return false;
        }
        return !!armed;
    }
    window.addEventListener('message', event => {
        if (event.source !== window || event.origin !== ORIGIN || event.data?.channel !== CHANNEL)
            return;
        const m = event.data;
        current();
        if (!/^[a-f0-9]{32}$/.test(m.nonce || ''))
            return;
        if (m.type === 'release') {
            if (armed?.nonce === m.nonce)
                finish();
            return;
        }
        if (m.type !== 'arm' || armed || location.origin !== ORIGIN || location.pathname !== '/news' || !/^#trader-saveticker=[a-f0-9]{32}:background$/.test(location.hash) || document.querySelectorAll('button[aria-label="PDF 다운로드"][data-trader-pdf-nonce="' + m.nonce + '"]').length !== 1)
            return;
        const s = { button: document.querySelector('button[aria-label="PDF 다운로드"][data-trader-pdf-nonce="' + m.nonce + '"]'), clicked: false, nonce: m.nonce, page: location.href, deadline: Date.now() + 15000, url: null, deferred: false, originalClick: HTMLAnchorElement.prototype.click, originalRevoke: URL.revokeObjectURL };
        s.click = function () {
            if (!current())
                return s.originalClick.call(this);
            if (!s.clicked)
                return s.originalClick.call(this);
            let u;
            try {
                if (this.href.length > 2048)
                    return s.originalClick.call(this);
                u = new URL(this.href);
            }
            catch {
                return s.originalClick.call(this);
            }
            if (u.protocol !== 'blob:' || u.origin !== ORIGIN)
                return s.originalClick.call(this);
            if (s.url) {
                finish('multiple_candidates');
                return;
            }
            s.url = u.href;
            emit('candidate', s.nonce, { url: s.url });
        };
        s.revoke = function (url) {
            if (!current())
                return s.originalRevoke.call(URL, url);
            if (url === s.url) {
                s.deferred = true;
                return;
            }
            return s.originalRevoke.call(URL, url);
        };
        s.interaction = e => { if (e.isTrusted) {
            finish('user_interaction');
            return;
        } if (e.type === 'click' && (e.target === s.button || s.button.contains(e.target)))
            s.clicked = true; };
        s.navigation = () => finish('navigation');
        armed = s;
        HTMLAnchorElement.prototype.click = s.click;
        URL.revokeObjectURL = s.revoke;
        for (const name of ['pointerdown', 'mousedown', 'click', 'keydown', 'touchstart', 'wheel'])
            document.addEventListener(name, s.interaction, true);
        for (const name of ['pagehide', 'popstate', 'hashchange'])
            window.addEventListener(name, s.navigation, true);
        s.timer = setTimeout(() => finish('expired'), 15000);
        emit('ready', s.nonce);
    });
})();
