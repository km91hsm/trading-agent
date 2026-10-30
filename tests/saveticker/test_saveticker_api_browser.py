"""Real hidden Chromium, production worker/content, fake port + real Native validator.

Only synthetic HTTPS and a temporary profile are used; no operator browser or
SaveTicker account. Playwright default focus/timer emulation is disabled by
manual launch + connect_over_cdp(no_defaults=True).
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from trader.saveticker.native import NativeSession


def test_hidden_production_api_pipeline(tmp_path):
    source = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    extension = tmp_path / "extension"
    shutil.copytree(source, extension)
    client = NativeSession(tmp_path / "collector")
    view = next(v for v in client._plan["views"] if v["stream"] == "news_all")
    job = client.handle({"id": 1, "type": "api_begin", "stream": "news_all"})
    assert job["ok"]
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["permissions"].remove("nativeMessaging")
    manifest["background"]["service_worker"] = "probe-worker.js"
    manifest_path.write_text(json.dumps(manifest))
    setup = """
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
globalThis.apiCalls=[];globalThis.focusCalls=[];
const update=chrome.windows.update.bind(chrome.windows);
chrome.windows.update=async(id,options)=>{
 if(options.focused)focusCalls.push(options);return update(id,options);};
chrome.runtime.connectNative=()=>{const p={onMessage:event(),onDisconnect:event(),disconnect(){},
 postMessage(m){
 apiCalls.push(m);let r={id:m.id,ok:true,status:'partial',count:1};
 if(m.type==='hello')Object.assign(r,{views:[VIEW]});
 if(m.type==='api_begin')Object.assign(r,JOB,{id:m.id});
 queueMicrotask(()=>p.onMessage.listeners.forEach(f=>f(r)));
}};return p;};
""".replace("VIEW", json.dumps(view)).replace("JOB", json.dumps(job))
    (extension / "probe-native.js").write_text(setup)
    (extension / "probe-worker.js").write_text(
        "import './probe-native.js';import {tick} from './worker.js';"
        "void (async()=>{await chrome.storage.local.set({enabled:false,backend:'api',"
        "ownerToken:'a'.repeat(32)});"
        "globalThis.runProbe=async()=>{const [tab]=await chrome.tabs.query({url:'https://www.saveticker.com/*'});"
        "await chrome.windows.update(tab.windowId,{state:'minimized'});"
        "await chrome.storage.local.set({enabled:true});await tick();"
        "return {calls:apiCalls,focus:focusCalls,window:await chrome.windows.get(tab.windowId)};};"
        "globalThis.probeReady=true;})();"
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    seen = []
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium is not installed")
        proc = subprocess.Popen(
            [
                playwright.chromium.executable_path,
                f"--user-data-dir={profile}",
                "--remote-debugging-port=0",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = None
        try:
            endpoint = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 10
            while not endpoint.exists():
                assert time.monotonic() < deadline, "isolated browser startup"
                time.sleep(0.05)
            port = endpoint.read_text().splitlines()[0]
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", no_defaults=True
            )
            context = browser.contexts[0]

            def route(request):
                if request.request.url == job["request"]["url"]:
                    seen.append("synthetic=yes" in request.request.headers.get("cookie", ""))
                    request.fulfill(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "news_list": [
                                    {
                                        "id": "1",
                                        "title": "Hidden news",
                                        "source": "save-pick",
                                        "author_secret": "never",
                                    }
                                ]
                            }
                        ),
                    )
                elif request.request.url == "https://www.saveticker.com/news":
                    request.fulfill(
                        content_type="text/html",
                        body="<html><title>API hidden probe</title><body>synthetic</body></html>",
                        headers={"Set-Cookie": "synthetic=yes; Secure; SameSite=Lax; Path=/"},
                    )
                else:
                    request.abort()

            context.route("**/*", route)
            page = context.new_page()
            page.goto("https://www.saveticker.com/news#trader-saveticker-api=" + "a" * 32)
            workers = context.service_workers
            worker = next((w for w in workers if w.url.endswith("/probe-worker.js")), None)
            if worker is None:
                worker = context.wait_for_event(
                    "serviceworker",
                    predicate=lambda w: w.url.endswith("/probe-worker.js"),
                    timeout=10000,
                )
            deadline = time.monotonic() + 10
            while not worker.evaluate("()=>globalThis.probeReady===true"):
                assert time.monotonic() < deadline, "probe worker ready"
                time.sleep(0.1)
            # Wait for document_idle listener without making a website request.
            deadline = time.monotonic() + 10
            while True:
                ready = worker.evaluate(
                    "async()=>{try{const [t]=await chrome.tabs.query({"
                    "url:'https://www.saveticker.com/*'});"
                    "return await chrome.tabs.sendMessage(t.id,{type:'api_fetch',"
                    "token:'a'.repeat(32),job:JOB});}catch{return null;}}".replace(
                        "JOB", json.dumps(job)
                    )
                )
                if ready and ready.get("error") == "api_stopped":
                    break
                assert time.monotonic() < deadline, (
                    f"content listener ready: {ready!r}; url={page.url}"
                )
                time.sleep(0.1)
            result = worker.evaluate("()=>runProbe()")
            assert result["window"]["state"] == "minimized"
            assert page.evaluate("document.visibilityState") == "hidden"
            assert result["focus"] == []
            messages = [m for m in result["calls"] if m["type"] == "api_snapshot"]
            assert len(messages) == 1 and seen == [True]
            assert "author_secret" not in json.dumps(messages[0])
            saved = client.handle(messages[0])
            assert saved["ok"] and saved["count"] == 1
            latest = json.loads((tmp_path / "collector/data/latest/news_all.json").read_text())
            assert latest["items"][0]["title"] == "Hidden news"
            assert latest["status"] == "partial"
        finally:
            if browser:
                browser.close()
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)


def test_production_api_recovers_after_isolated_browser_restart(tmp_path):
    """Restart only the disposable browser; preserve token, pacing and auth block."""
    source = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    extension = tmp_path / "restart-extension"
    shutil.copytree(source, extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["permissions"].remove("nativeMessaging")
    manifest["background"]["service_worker"] = "restart-worker.js"
    manifest_path.write_text(json.dumps(manifest))
    request_url = (
        "https://www.saveticker.com/api/news/list?"
        "page=1&page_size=20&sort=created_at_desc&label_group=1&label_name=1"
    )
    # A fake local Native port, never the operator's host. The production worker
    # and content script remain intact and their persisted schedule is real.
    (extension / "restart-native.js").write_text(
        """
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
globalThis.restartCalls=[];
chrome.runtime.connectNative=()=>{const p={onMessage:event(),onDisconnect:event(),
 disconnect(){},postMessage(m){
 restartCalls.push(m);let r={id:m.id,ok:true,status:'partial',count:1};
 if(m.type==='hello')r.views=[{stream:'news_all',kind:'news',interval_s:30,
  url:'https://www.saveticker.com/news'}];
 if(m.type==='api_begin')Object.assign(r,{job_id:crypto.randomUUID().replaceAll('-',''),
  expires_at:new Date(Date.now()+120000).toISOString(),
  request:{method:'GET',kind:'news',max_bytes:2097152,url:URL_VALUE}});
 queueMicrotask(()=>p.onMessage.listeners.forEach(fn=>fn(r)));
}};return p;};
""".replace("URL_VALUE", json.dumps(request_url))
    )
    (extension / "restart-worker.js").write_text(
        "import './restart-native.js';import {tick} from './worker.js';"
        "globalThis.productionTick=tick;globalThis.restartReady=true;"
    )
    profile = tmp_path / "restart-profile"
    profile.mkdir()
    observations = []
    token = "c" * 32
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium is not installed")

        def launch():
            endpoint = profile / "DevToolsActivePort"
            endpoint.unlink(missing_ok=True)  # Disposable profile, previous port only.
            proc = subprocess.Popen(
                [
                    playwright.chromium.executable_path,
                    f"--user-data-dir={profile}",
                    "--remote-debugging-port=0",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            browser = None
            try:
                deadline = time.monotonic() + 10
                while not endpoint.exists():
                    assert time.monotonic() < deadline, "restart browser startup"
                    time.sleep(0.05)
                port = endpoint.read_text().splitlines()[0]
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}", no_defaults=True
                )
                context = browser.contexts[0]

                def route(request):
                    if request.request.url.startswith("chrome-extension://"):
                        request.continue_()
                    elif request.request.url == request_url:
                        observations.append(time.time())
                        request.fulfill(
                            content_type="application/json",
                            body=json.dumps(
                                {"news_list": [{"id": "restart", "title": "Restart proof"}]}
                            ),
                        )
                    elif request.request.url == "https://www.saveticker.com/news":
                        request.fulfill(
                            content_type="text/html", body="<title>Restart probe</title>"
                        )
                    else:
                        request.abort()

                context.route("**/*", route)
                worker = next(
                    (w for w in context.service_workers if w.url.endswith("/restart-worker.js")),
                    None,
                )
                if worker is None:
                    worker = context.wait_for_event(
                        "serviceworker",
                        predicate=lambda w: w.url.endswith("/restart-worker.js"),
                        timeout=10000,
                    )
                deadline = time.monotonic() + 10
                while not worker.evaluate("()=>globalThis.restartReady===true"):
                    assert time.monotonic() < deadline, (
                        "restart worker ready "
                        + worker.url
                        + " "
                        + str(
                            worker.evaluate(
                                "()=>({ready:globalThis.restartReady,calls:globalThis.restartCalls,"
                                "tick:typeof globalThis.productionTick})"
                            )
                        )
                    )
                    time.sleep(0.1)
                return proc, browser, context, worker
            except BaseException:
                if browser is not None:
                    browser.close()
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
                raise

        def close(proc, browser):
            browser.close()
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)

        proc, browser, context, worker = launch()
        try:
            page = context.new_page()
            page.goto("https://www.saveticker.com/news#trader-saveticker-api=" + token)
            worker.evaluate(
                "async token=>{await chrome.storage.local.set({enabled:true,"
                "backend:'api',ownerToken:token});await productionTick();}",
                token,
            )
            # The listener may not yet be at document_idle on the first call.
            deadline = time.monotonic() + 12
            while not worker.evaluate("()=>restartCalls.some(m=>m.type==='api_snapshot')"):
                assert time.monotonic() < deadline, "first API save"
                worker.evaluate("()=>productionTick()")
                time.sleep(0.2)
            before = worker.evaluate("()=>chrome.storage.local.get(null)")
            assert before["collectionSchedule"]["entries"][0]["stream"] == "news_all"
            first_count = len(observations)
        finally:
            close(proc, browser)

        proc, browser, context, worker = launch()
        try:
            resumed = worker.evaluate("()=>chrome.storage.local.get(null)")
            assert resumed["enabled"] and resumed["backend"] == "api"
            assert resumed["ownerToken"] == token
            assert resumed["collectionSchedule"] == before["collectionSchedule"]
            assert len(observations) == first_count, "restart must respect saved interval"
            deadline = time.monotonic() + 45
            while not worker.evaluate("()=>restartCalls.some(m=>m.type==='api_snapshot')"):
                assert time.monotonic() < deadline, "automatic API recovery after restart"
                time.sleep(0.5)
            assert len(observations) > first_count
            assert observations[first_count] - observations[first_count - 1] >= 29
            # Persist a security pause, then restart again. It must not create a
            # page, call api_begin, or require another user action to stay paused.
            worker.evaluate(
                "()=>chrome.storage.local.set({hostBlocked:true,"
                "blockedLanes:['news','community','market','background']})"
            )
            blocked_count = len(observations)
        finally:
            close(proc, browser)

        proc, browser, context, worker = launch()
        try:
            worker.evaluate("()=>productionTick()")
            state = worker.evaluate("()=>chrome.storage.local.get(null)")
            assert state["hostBlocked"] is True
            assert not worker.evaluate("()=>restartCalls.some(m=>m.type==='api_begin')")
            assert len(observations) == blocked_count
        finally:
            close(proc, browser)
