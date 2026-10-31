// Static MAIN-world DOM handoff. No network or application-state observation.
(() => {
    const CHANNEL = 'trader-saveticker-pdf';
    const ORIGIN = 'https://www.saveticker.com';
    const INTERACTIONS = ['pointerdown', 'mousedown', 'click', 'keydown', 'touchstart', 'wheel'];
    const NAVIGATION = ['pagehide', 'popstate', 'hashchange'];
    let armed = null;
    const emit = (type, nonce, extra = {}) => window.postMessage({channel: CHANNEL, type, nonce, ...extra}, ORIGIN);

    function install(object, name, wrapper, original) {
        Object.defineProperty(object, name, {
            value: wrapper,
            configurable: original?.configurable ?? true,
            enumerable: original?.enumerable ?? false,
            writable: original && 'writable' in original ? original.writable : true
        });
    }
    function restore(object, name, wrapper, original) {
        if (object[name] !== wrapper) return;
        if (original) Object.defineProperty(object, name, original);
        else delete object[name];
    }
    function finish(reason) {
        const s = armed;
        if (!s) return;
        armed = null;
        clearTimeout(s.timer);
        restore(HTMLAnchorElement.prototype, 'click', s.click, s.clickDescriptor);
        restore(HTMLAnchorElement.prototype, 'dispatchEvent', s.dispatch, s.dispatchDescriptor);
        restore(URL, 'revokeObjectURL', s.revoke, s.revokeDescriptor);
        for (const name of INTERACTIONS) document.removeEventListener(name, s.interaction, true);
        for (const name of NAVIGATION) window.removeEventListener(name, s.navigation, true);
        if (s.deferred && s.url) s.originalRevoke.call(URL, s.url);
        if (reason) emit('aborted', s.nonce, {reason});
    }
    function current() {
        if (armed && (Date.now() >= armed.deadline || location.href !== armed.page)) {
            finish(Date.now() >= armed.deadline ? 'expired' : 'navigation');
            return false;
        }
        return !!armed;
    }
    function capture(s, anchor) {
        if (armed !== s || !current() || !s.clicked || !(anchor instanceof HTMLAnchorElement)) return false;
        let u;
        try {
            if (anchor.href.length > 2048) return false;
            u = new URL(anchor.href);
        } catch { return false; }
        if (u.protocol !== 'blob:' || u.origin !== ORIGIN) return false;
        if (s.url) {
            finish('multiple_candidates');
            return true;
        }
        s.url = u.href;
        emit('candidate', s.nonce, {url: s.url});
        return true;
    }
    window.addEventListener('message', event => {
        if (event.source !== window || event.origin !== ORIGIN || event.data?.channel !== CHANNEL) return;
        const m = event.data;
        current();
        if (!/^[a-f0-9]{32}$/.test(m.nonce || '')) return;
        if (m.type === 'release') {
            if (armed?.nonce === m.nonce) finish();
            return;
        }
        const buttons = document.querySelectorAll('button[aria-label="PDF 다운로드"][data-trader-pdf-nonce="' + m.nonce + '"]');
        if (m.type !== 'arm' || armed || location.origin !== ORIGIN || location.pathname !== '/news' ||
            !/^#trader-saveticker=[a-f0-9]{32}:background$/.test(location.hash) || buttons.length !== 1) return;
        const prototype = HTMLAnchorElement.prototype;
        const s = {
            button: buttons[0], clicked: false, nonce: m.nonce, page: location.href,
            deadline: Date.now() + 15000, url: null, deferred: false,
            originalClick: prototype.click, originalDispatch: prototype.dispatchEvent,
            originalRevoke: URL.revokeObjectURL,
            clickDescriptor: Object.getOwnPropertyDescriptor(prototype, 'click'),
            dispatchDescriptor: Object.getOwnPropertyDescriptor(prototype, 'dispatchEvent'),
            revokeDescriptor: Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')
        };
        s.click = function () {
            if (capture(s, this)) return;
            return s.originalClick.call(this);
        };
        s.dispatch = function (event) {
            if (event instanceof MouseEvent && event.type === 'click' && !event.isTrusted &&
                !event.defaultPrevented && capture(s, this)) {
                if (event.cancelable) event.preventDefault();
                return !event.defaultPrevented;
            }
            return s.originalDispatch.call(this, event);
        };
        s.revoke = function (url) {
            if (armed !== s || !current()) return s.originalRevoke.call(URL, url);
            if (url === s.url) { s.deferred = true; return; }
            return s.originalRevoke.call(URL, url);
        };
        s.interaction = event => {
            if (event.isTrusted) { finish('user_interaction'); return; }
            if (event.type === 'click' && (event.target === s.button || s.button.contains(event.target))) s.clicked = true;
        };
        s.navigation = () => finish('navigation');
        armed = s;
        try {
            install(prototype, 'click', s.click, s.clickDescriptor);
            install(prototype, 'dispatchEvent', s.dispatch, s.dispatchDescriptor);
            install(URL, 'revokeObjectURL', s.revoke, s.revokeDescriptor);
        } catch { finish('hook_unavailable'); return; }
        for (const name of INTERACTIONS) document.addEventListener(name, s.interaction, true);
        for (const name of NAVIGATION) window.addEventListener(name, s.navigation, true);
        s.timer = setTimeout(() => finish('expired'), 15000);
        emit('ready', s.nonce);
    });
})();
