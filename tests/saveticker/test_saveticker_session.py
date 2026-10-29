import json
import os
import threading

import pytest
from helpers_saveticker import launch_or_skip

from trader.saveticker.browser import Browser, VerificationRequired


@pytest.fixture
def chromium():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as p:
        browser = launch_or_skip(p)
        yield browser
        browser.close()


def test_session_reuses_cookies_and_local_storage_under_lock(chromium, tmp_path, monkeypatch):
    from trader.platform import filelock
    from trader.saveticker import session

    signed_in = []

    class Login(Browser):
        def login(self):
            lock = filelock.try_hold(tmp_path / "login.lock")
            assert lock is None
            self.page.route("https://www.saveticker.com/**", lambda r: r.fulfill(body="ok"))
            self.page.goto("https://www.saveticker.com/profile")
            signed_in.append(self.page.evaluate("localStorage.getItem('session')"))
            previous_idb = self.page.evaluate("""async () => {
                const db = await new Promise((resolve, reject) => {
                    const req = indexedDB.open('auth', 1);
                    req.onupgradeneeded = () => req.result.createObjectStore('tokens');
                    req.onsuccess = () => resolve(req.result);
                    req.onerror = () => reject(req.error);
                });
                const result = await new Promise((resolve, reject) => {
                    const tx = db.transaction('tokens', 'readwrite');
                    const store = tx.objectStore('tokens');
                    const req = store.get('session');
                    let previous;
                    req.onsuccess = () => { previous = req.result ?? null; };
                    store.put('synthetic-idb-token', 'session');
                    tx.oncomplete = () => resolve(previous);
                    tx.onerror = () => reject(tx.error);
                });
                db.close();
                return result;
            }""")
            assert previous_idb == (None if len(signed_in) == 1 else "synthetic-idb-token")

            self.page.evaluate("localStorage.setItem('session', 'synthetic-token')")
            self.page.context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "synthetic-cookie",
                        "domain": "www.saveticker.com",
                        "path": "/",
                    }
                ]
            )

    monkeypatch.setattr(session, "Browser", Login)
    for _ in range(2):
        with session.session_context(
            chromium,
            tmp_path,
            ("test@example.com", "pw"),
            headed=False,
            stop=threading.Event(),
            on_wait=lambda: None,
        ) as adapter:
            assert adapter.page.context.cookies()[0]["value"] == "synthetic-cookie"
        assert chromium.contexts == []
    assert signed_in == [None, "synthetic-token"]
    path = tmp_path / "auth" / "storage-state.json"
    state = json.loads(path.read_text())
    assert state["cookies"][0]["name"] == "session"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["verification", "timeout", "login"])
def test_manual_verification_waits_without_resubmitting_and_saves(
    chromium,
    tmp_path,
    monkeypatch,
    failure,
):
    from playwright.sync_api import TimeoutError as BrowserTimeout

    from trader.saveticker import session
    from trader.saveticker.browser import LoginRequired

    error = {
        "verification": VerificationRequired,
        "timeout": BrowserTimeout,
        "login": LoginRequired,
    }[failure]
    submitted = []

    class Verify(Browser):
        def login(self):
            submitted.append(True)
            self.page.set_content("<p>재확인이 필요합니다</p>")
            raise error("synthetic_login_failure")

    monkeypatch.setattr(session, "Browser", Verify)

    def complete():
        chromium.contexts[0].pages[0].set_content('<button aria-label="프로필 메뉴">me</button>')

    with session.session_context(
        chromium,
        tmp_path,
        ("test@example.com", "pw"),
        headed=True,
        stop=threading.Event(),
        on_wait=complete,
    ) as adapter:
        assert adapter.authenticated()
    assert submitted == [True]
    assert (tmp_path / "auth" / "storage-state.json").exists()


@pytest.mark.parametrize("reason", ["headless", "timeout", "stop"])
def test_verification_failure_preserves_state_and_closes_context(
    chromium,
    tmp_path,
    monkeypatch,
    reason,
):
    from trader.saveticker import session

    path = tmp_path / "auth" / "storage-state.json"
    path.parent.mkdir()
    original = '{"cookies": [], "origins": []}'
    path.write_text(original)

    class Verify(Browser):
        def login(self):
            raise VerificationRequired()

    monkeypatch.setattr(session, "Browser", Verify)
    monkeypatch.setattr(session, "MANUAL_LOGIN_TIMEOUT_S", 0 if reason == "timeout" else 600)
    stop = threading.Event()
    if reason == "stop":
        stop.set()
    with pytest.raises(InterruptedError if reason == "stop" else VerificationRequired):
        with session.session_context(
            chromium,
            tmp_path,
            ("test@example.com", "pw"),
            headed=reason != "headless",
            stop=stop,
            on_wait=lambda: None,
        ):
            pytest.fail("unauthenticated session was allowed")
    assert path.read_text() == original
    assert chromium.contexts == []


def test_initial_login_failure_retains_diagnostics(chromium, tmp_path, monkeypatch):
    from trader.saveticker import session

    seen = []

    class Denied(Browser):
        def login(self):
            self.stage = "login_result"
            self.diagnostics["verification_visible"] = True
            raise VerificationRequired()

    monkeypatch.setattr(session, "Browser", Denied)
    with pytest.raises(VerificationRequired):
        with session.session_context(
            chromium,
            tmp_path,
            ("test@example.com", "pw"),
            headed=False,
            stop=threading.Event(),
            on_wait=lambda: None,
            on_adapter=seen.append,
        ):
            pass
    assert seen[0].stage == "login_result"
    assert seen[0].diagnostics["verification_visible"] is True


def test_waiting_for_login_lock_can_be_cancelled(tmp_path):
    from trader.platform import filelock
    from trader.saveticker import session

    stop = threading.Event()
    held = filelock.try_hold(tmp_path / "login.lock")
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            with session.login_lock(tmp_path, stop=stop):
                pytest.fail("lock owner is still active")
    finally:
        timer.cancel()
        filelock.release_hold(held)


def test_collection_uses_session_reauthentication(chromium, tmp_path, monkeypatch):
    from trader.saveticker import session
    from trader.saveticker.service import View

    calls = []

    class VerifyOnExpiry(Browser):
        def login(self):
            calls.append(True)
            if len(calls) > 1:
                self.page.set_content("<p>재확인이 필요합니다</p>")
                raise VerificationRequired()

    monkeypatch.setattr(session, "Browser", VerifyOnExpiry)

    def complete():
        chromium.contexts[0].pages[0].set_content('<button aria-label="프로필 메뉴">me</button>')

    with session.session_context(
        chromium,
        tmp_path,
        ("test@example.com", "pw"),
        headed=True,
        stop=threading.Event(),
        on_wait=complete,
    ) as adapter:
        adapter.page.route(
            "https://www.saveticker.com/**",
            lambda r: r.fulfill(
                body='<button aria-label="프로필 메뉴">me</button>'
                if len(calls) > 1
                else "<p>로그인 필요</p>",
                content_type="text/html; charset=utf-8",
            ),
        )
        monkeypatch.setattr(adapter.page, "wait_for_timeout", lambda ms: None)
        result = adapter.collect(View("market_gain_live", "market"))
        assert result.status == "partial"
        assert calls == [True, True]
        assert adapter.authenticated()
