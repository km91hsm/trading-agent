"""Real isolated-world content -> static MAIN probe; synthetic HTTPS only.

Run directly with ``uv run python tests/saveticker/test_saveticker_pdf_content_browser.py``.
The temporary extension replaces its worker, removes Native Messaging permission,
and never uses the operator's Chrome profile or the real SaveTicker service.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

_OWNER = "b" * 32
_NONCE = "a" * 32
_TITLE = "2026년 10월 29일 (목요일)"

_WORKER = """
void chrome.storage.local.set({enabled:false});
globalThis.probePaces = [];
// This bridge-only fixture tests IPC and DOM provenance, not delay duration.
// The production-worker browser test covers actual pacing. Keep fake replies
// short so an unconnected test worker cannot outlive the bounded MAIN lease.
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
    if(message.type==='pace'){
        const pace={sameExtension:sender.id===chrome.runtime.id,
                    tokenMatches:message.ownerToken==='b'.repeat(32),at:Date.now()};
        globalThis.probePaces.push(pace);
        setTimeout(()=>{pace.repliedAt=Date.now();reply({ok:true});},100);
        return true;
    }
    if(message.type==='pdf_abort'){reply({ok:true});return;}
});
"""

_PAGE_SCRIPT = """
window.probeMessages=[];window.probeClicks=0;
window.addEventListener('message',event=>{
    if(event.data?.channel!=='trader-saveticker-pdf')return;
    window.probeMessages.push({type:event.data.type,origin:event.origin,
        sourceWindow:event.source===window,nonceMatches:event.data.nonce==='a'.repeat(32),
        reason:event.data.reason,at:Date.now()});
});
document.querySelector('[aria-label="PDF 다운로드"]').addEventListener('click',()=>{
    window.probeClicks++;
    const anchor=document.createElement('a');
    anchor.href=URL.createObjectURL(new Blob(['%PDF-1.4\\nSYNTHETIC CONTENT PROBE'],
                                             {type:'application/pdf'}));
    anchor.download='synthetic-content-probe.pdf';
    anchor.click();
    URL.revokeObjectURL(anchor.href);
});
"""


def _probe_content_handoff(tmp_path: Path, *, dispatch: bool = False) -> dict:
    page_script = (
        _PAGE_SCRIPT.replace(
            "anchor.click();",
            "anchor.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true}));",
        )
        if dispatch
        else _PAGE_SCRIPT
    )
    source = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    extension = tmp_path / "extension"
    extension.mkdir()
    for path in source.iterdir():
        if path.is_file() and path.suffix in {".js", ".json", ".html", ".css"}:
            shutil.copy2(path, extension / path.name)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["permissions"] = ["storage"]
    manifest["name"] = "SaveTicker isolated content diagnostic"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    (extension / "worker.js").write_text(_WORKER)
    html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"></head><body>'
        '<button aria-label="프로필 메뉴">프로필</button>'
        f'<article><p>{_TITLE}</p><button aria-label="PDF 다운로드">PDF</button></article>'
        '<script src="/content-probe.js"></script></body></html>'
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
                url = request.request.url
                if not url.startswith("https://www.saveticker.com/"):
                    request.abort()
                elif url.endswith("/content-probe.js"):
                    request.fulfill(
                        status=200, content_type="application/javascript", body=page_script
                    )
                else:
                    request.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        headers={
                            "Content-Security-Policy": "default-src 'self'; script-src 'self'"
                        },
                        body=html,
                    )

            context.route("**/*", route)
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker", timeout=15000)
            )
            assert worker.evaluate("async()=>!(await chrome.storage.local.get('enabled')).enabled")
            page = context.new_page()
            page.goto(
                "https://www.saveticker.com/news?label_group=7#trader-saveticker="
                + _OWNER
                + ":background",
                wait_until="load",
            )
            page.wait_for_selector('[aria-label="PDF 다운로드"]')
            reply = worker.evaluate(
                """async ({owner,nonce,title})=>{
                    const tabs=await chrome.tabs.query({url:'https://www.saveticker.com/*'});
                    const marker='#trader-saveticker='+owner+':background';
                    const tab=tabs.find(t=>t.url?.endsWith(marker));
                    for(let i=0;i<10;i++){
                        try{return await Promise.race([chrome.tabs.sendMessage(tab.id,
                            {type:'pdf_click',token:owner,nonce,title}),
                            new Promise(r=>setTimeout(()=>r(
                                {ok:false,reason:'content_reply_timeout'}),12000))]);}
                        catch{await new Promise(r=>setTimeout(r,100));}
                    }
                    return {ok:false,reason:'content_listener_unavailable'};
                }""",
                {"owner": _OWNER, "nonce": _NONCE, "title": _TITLE},
            )
            events = page.evaluate("()=>window.probeMessages")
            paces = worker.evaluate("()=>globalThis.probePaces")
            result = {
                "reply_ok": reply.get("ok") is True,
                "reply_reason": reply.get("reason"),
                "candidate_is_same_origin_blob": str(reply.get("observed_url", "")).startswith(
                    "blob:https://www.saveticker.com/"
                ),
                "nonce_matches": reply.get("nonce") == _NONCE,
                "pace_requests": len(paces),
                "pace_timings": paces,
                "pace_authentication": all(p["sameExtension"] and p["tokenMatches"] for p in paces),
                "page_button_clicks": page.evaluate("()=>window.probeClicks"),
                "events": events,
            }
            (tmp_path / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            worker.evaluate(
                """async ({owner,nonce})=>{
                    const [tab]=await chrome.tabs.query({url:'https://www.saveticker.com/*'});
                    await chrome.tabs.sendMessage(tab.id,{type:'pdf_release',token:owner,nonce});
                }""",
                {"owner": _OWNER, "nonce": _NONCE},
            )
            return result
        finally:
            context.close()


@pytest.mark.parametrize("dispatch", [False, True], ids=["anchor-click", "anchor-dispatch"])
def test_content_to_static_main_pdf_candidate_in_isolated_chromium(tmp_path, dispatch):
    result = _probe_content_handoff(tmp_path, dispatch=dispatch)
    assert result["reply_ok"], json.dumps(result, ensure_ascii=False)
    assert result["candidate_is_same_origin_blob"]
    assert result["nonce_matches"]
    assert result["pace_requests"] == 3
    assert result["pace_authentication"]
    assert result["page_button_clicks"] == 1
    assert {event["type"] for event in result["events"]} >= {"arm", "ready", "candidate"}


if __name__ == "__main__":
    directory = Path(tempfile.mkdtemp(prefix="saveticker-content-probe-"))
    diagnostic = _probe_content_handoff(directory)
    print(json.dumps({"directory": str(directory), **diagnostic}, ensure_ascii=False, indent=2))
    assert diagnostic["reply_ok"] and diagnostic["candidate_is_same_origin_blob"]
