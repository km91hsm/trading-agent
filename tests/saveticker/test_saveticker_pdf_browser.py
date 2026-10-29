"""Isolated Chrome capability check; no real website, profile or Native host is used."""

import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


def test_main_blob_handoff_can_start_exact_chrome_download(tmp_path):
    extension = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    nonce = "a" * 32
    html = (
        '<html><head><meta charset="utf-8"></head><body>'
        '<button aria-label="PDF 다운로드" '
        f'data-trader-pdf-nonce="{nonce}">PDF</button></body></html>'
    )
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium is not installed")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            channel="chromium",
            headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            downloads_path=str(tmp_path / "downloads"),
            accept_downloads=True,
        )
        try:

            def route(request):
                if request.request.url.startswith("https://www.saveticker.com/"):
                    request.fulfill(
                        status=200,
                        content_type="text/html",
                        headers={
                            "Content-Security-Policy": "default-src 'self'; script-src 'self'"
                        },
                        body=html,
                    )
                else:
                    request.abort()

            context.route("**/*", route)
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker", timeout=15000)
            )
            assert worker.evaluate(
                "async () => !(await chrome.storage.local.get('enabled')).enabled"
            ), "The collector must stay disabled in this isolated profile"
            page = context.new_page()
            page.goto(
                "https://www.saveticker.com/news#trader-saveticker=" + "b" * 32 + ":background",
                wait_until="domcontentloaded",
            )
            candidate = page.evaluate("""async () => {
                const nonce = 'a'.repeat(32);
                return await new Promise(resolve => {
                    const timer = setTimeout(() => resolve(null), 5000);
                    window.addEventListener('message', event => {
                        const m = event.data;
                        if (m?.channel !== 'trader-saveticker-pdf' || m.nonce !== nonce) return;
                        if (m.type === 'ready') {
                            const button = document.querySelector('[aria-label="PDF 다운로드"]');
                            button.addEventListener('click', () => {
                                const anchor = document.createElement('a');
                                anchor.href = URL.createObjectURL(new Blob(
                                    ['%PDF-1.4\\nSYNTHETIC TEST ONLY'], {type:'application/pdf'}));
                                anchor.download = 'synthetic.pdf';
                                anchor.click();
                                URL.revokeObjectURL(anchor.href);
                            });
                            button.click();
                        }
                        if (m.type === 'candidate') { clearTimeout(timer); resolve(m.url); }
                    });
                    window.postMessage({channel:'trader-saveticker-pdf',type:'arm',nonce},
                                       location.origin);
                });
            }""")
            assert candidate and candidate.startswith("blob:https://www.saveticker.com/")
            download_id = worker.evaluate(
                """async url => await chrome.downloads.download({
                url, filename:'SaveTicker/synthetic-probe.pdf', saveAs:false
            })""",
                candidate,
            )
            assert isinstance(download_id, int)
            deadline = time.monotonic() + 15
            items = []
            while time.monotonic() < deadline:
                items = worker.evaluate(
                    "async id => await chrome.downloads.search({id})", download_id
                )
                if items and items[0]["state"] in {"complete", "interrupted"}:
                    break
                time.sleep(0.1)
            assert items and items[0]["state"] == "complete"
            assert items[0]["bytesReceived"] > 0
            page.evaluate("""() => window.postMessage({channel:'trader-saveticker-pdf',
                type:'release',nonce:'a'.repeat(32)}, location.origin)""")
        finally:
            context.close()
