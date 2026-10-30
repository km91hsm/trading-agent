"""Synthetic hidden API → real browser download → real Native PDF validation.

The fake Native port is transported through intercepted synthetic HTTPS requests;
there is no test HTTP listener, no operator profile, and DNS is blocked.
"""

import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pypdf import PdfWriter

from trader.saveticker.native import NativeSession


def test_hidden_api_pdf_uses_real_native_ticket_and_attachment(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    extension = tmp_path / "extension"
    shutil.copytree(source, extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["permissions"].remove("nativeMessaging")
    manifest["background"]["service_worker"] = "native-http-worker.js"
    manifest_path.write_text(json.dumps(manifest))
    (extension / "native-http.js").write_text("""
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
globalThis.nativeTransportErrors=[];
chrome.runtime.connectNative=()=>{const p={onMessage:event(),onDisconnect:event(),
 disconnect(){},postMessage(message){
 fetch('https://www.saveticker.com/__native-test',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(message)})
 .then(r=>r.json()).then(reply=>p.onMessage.listeners.forEach(fn=>fn(reply)))
 .catch(()=>{nativeTransportErrors.push('synthetic_transport_failed');
  p.onDisconnect.listeners.forEach(fn=>fn());});
}};return p;};
""")
    (extension / "native-http-worker.js").write_text(
        "import './native-http.js';import {tick} from './worker.js';"
        "void(async()=>{await chrome.storage.local.set({enabled:false,backend:'api',"
        "ownerToken:'d'.repeat(32)});globalThis.apiPdfReady=true;"
        "globalThis.runApiPdf=async()=>{const [tab]=await chrome.tabs.query({"
        "url:'https://www.saveticker.com/*'});"
        "await chrome.windows.update(tab.windowId,{state:'minimized'});"
        "await chrome.storage.local.set({enabled:true});await tick();await tick();"
        "return {state:await chrome.storage.local.get(null),"
        "window:await chrome.windows.get(tab.windowId),errors:nativeTransportErrors};};})();"
    )
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    client = NativeSession(tmp_path / "collector")
    native_messages, network = [], []
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    pdf_bytes = buffer.getvalue()
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default/Preferences").write_text(
        json.dumps(
            {
                "download": {
                    "default_directory": str(downloads),
                    "prompt_for_download": False,
                    "directory_upgrade": True,
                }
            }
        )
    )
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
                assert time.monotonic() < deadline, "temporary browser startup"
                time.sleep(0.05)
            port = endpoint.read_text().splitlines()[0]
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", no_defaults=True
            )
            context = browser.contexts[0]

            def route(request):
                url = request.request.url
                if url.startswith("chrome-extension://"):
                    request.continue_()
                    return
                if url == "https://www.saveticker.com/__native-test":
                    message = request.request.post_data_json
                    native_messages.append(message)
                    reply = client.handle(message)
                    if message["type"] == "hello" and reply["ok"]:
                        reply["views"] = [
                            v for v in reply["views"] if v["stream"] == "news_reports"
                        ]
                    request.fulfill(content_type="application/json", body=json.dumps(reply))
                    return
                network.append(url)
                if url == "https://www.saveticker.com/news":
                    request.fulfill(content_type="text/html", body="<title>API PDF test</title>")
                elif url == "https://www.saveticker.com/api/stocks/api/v1/watchlist/groups":
                    request.fulfill(
                        content_type="application/json",
                        body=json.dumps(
                            {"groups": [{"groupId": 42, "isDefault": True, "name": "private"}]}
                        ),
                    )
                elif url == "https://www.saveticker.com/api/report/list?page=1&page_size=20":
                    request.fulfill(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "reports": [
                                    {"id": "7", "title": "2026년 10월 30일 (금)", "has_pdf": True}
                                ],
                                "page": 1,
                                "page_size": 20,
                                "total_count": 1,
                            }
                        ),
                    )
                elif url == "https://www.saveticker.com/api/report/detail?id=7":
                    request.fulfill(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "report": {
                                    "id": "7",
                                    "title": "2026년 10월 30일 (금)",
                                    "pdf_url": "/api/uploads/documents/77",
                                }
                            }
                        ),
                    )
                elif (
                    url
                    == "https://www.saveticker.com/api/report/download?url=%2Fapi%2Fuploads%2Fdocuments%2F77"
                ):
                    request.fulfill(content_type="application/pdf", body=pdf_bytes)
                else:
                    request.abort()

            context.route("**/*", route)
            page = context.new_page()
            page.goto("https://www.saveticker.com/news#trader-saveticker-api=" + "d" * 32)
            worker = next(
                (w for w in context.service_workers if w.url.endswith("/native-http-worker.js")),
                None,
            )
            if worker is None:
                worker = context.wait_for_event(
                    "serviceworker",
                    predicate=lambda w: w.url.endswith("/native-http-worker.js"),
                    timeout=10000,
                )
            deadline = time.monotonic() + 10
            while not worker.evaluate("()=>globalThis.apiPdfReady===true"):
                assert time.monotonic() < deadline, "test worker ready"
                time.sleep(0.1)
            result = worker.evaluate("()=>runApiPdf()")
            assert result["errors"] == []
            assert result["window"]["state"] == "minimized"
            assert page.evaluate("document.visibilityState") == "hidden"
            assert result["state"].get("pdfState") == "saved", result
            latest = json.loads((tmp_path / "collector/data/latest/news_reports.json").read_text())
            attachment = latest["items"][0]["attachment"]
            assert attachment["pages"] == 1
            assert (tmp_path / "collector/data" / attachment["pdf_path"]).read_bytes() == pdf_bytes
            assert len([m for m in native_messages if m["type"] == "pdf"]) == 1
            assert "private" not in json.dumps(native_messages)
            assert len([u for u in network if "/api/report/download?" in u]) == 1
            (tmp_path / "acceptance.json").write_text(
                json.dumps(
                    {
                        "phase": "synthetic_hidden_api_pdf_real_native",
                        "ok": True,
                        "window_state": "minimized",
                        "visibility": "hidden",
                        "native_pdf_saved": True,
                        "pages": attachment["pages"],
                    }
                )
            )
        finally:
            if browser:
                browser.close()
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
