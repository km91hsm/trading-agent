"""Production worker PDF handoff with a fake Native port and synthetic HTTPS only."""

import json
import shutil
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

_NAME = "b" * 32 + "-1234567890123.pdf"
_TITLE = "2026년 10월 29일 (목요일)"
_WRAPPER = """
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn)}});
const view={stream:'news_reports',kind:'news',source:'SAVE 리포트',
 url:'https://www.saveticker.com/news',interval_s:900};
globalThis.nativeCalls=[];
globalThis.commandResults=[];
globalThis.filenameEvents=[];globalThis.filenameRegistrations=0;
const add=chrome.downloads.onDeterminingFilename.addListener.bind(
 chrome.downloads.onDeterminingFilename);
chrome.downloads.onDeterminingFilename.addListener=fn=>{
 filenameRegistrations++;return add(fn);
};
const send=chrome.tabs.sendMessage.bind(chrome.tabs);
chrome.tabs.sendMessage=async(...args)=>{
 const result=await send(...args);commandResults.push({type:args[1].type,result});return result;
};
chrome.runtime.connectNative=()=>{
 const port={onMessage:event(),onDisconnect:event(),disconnect(){},postMessage(m){
  nativeCalls.push(m);
  let r={id:m.id,ok:true,status:'partial',count:1};
  if(m.type==='hello')Object.assign(r,{views:[view],limits:{max_scrolls:0,max_details:0,max_reports:1}});
  if(m.type==='begin')Object.assign(r,{view,job_id:'synthetic-job'});
  if(m.type==='snapshot')r.report_items=[{id:'synthetic-report',title:TITLE,attachment_ok:false}];
  if(m.type==='pdf_begin')Object.assign(r,{ticket:'synthetic-ticket',download_name:NAME});
  queueMicrotask(()=>port.onMessage.listeners.forEach(fn=>fn(r)));
 }};
 return port;
};
void (async()=>{
await chrome.storage.local.set({enabled:false,ownerToken:'b'.repeat(32)});

globalThis.runProbe=async()=>{
 await chrome.storage.local.set({enabled:true});await productionTick();
};
globalThis.probeReady=true;
})();
""".replace("TITLE", json.dumps(_TITLE)).replace("NAME", json.dumps(_NAME))


def test_production_worker_blob_download_uses_issued_filename(tmp_path):
    source = Path(__file__).resolve().parents[2] / "extensions/saveticker"
    extension = tmp_path / "extension"
    shutil.copytree(source, extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["permissions"].remove("nativeMessaging")
    manifest["background"]["service_worker"] = "probe-worker.js"
    manifest_path.write_text(json.dumps(manifest))
    native_setup, runner = _WRAPPER.split("void (async()=>{", 1)
    (extension / "probe-native.js").write_text(native_setup)
    (extension / "probe-worker.js").write_text(
        "import './probe-native.js';\n"
        "import {tick as productionTick} from './worker.js';\n"
        "void (async()=>{" + runner
    )
    html = (
        '<html><head><meta charset="utf-8"></head><body>'
        '<button aria-label="프로필 메뉴">프로필</button>'
        '<button role="tab" aria-selected="true">SAVE 리포트</button>'
        f'<article><p>{_TITLE}</p><button aria-label="PDF 다운로드">PDF</button></article>'
        '<script src="/probe.js"></script></body></html>'
    )
    script = """
    document.querySelector('[aria-label="PDF 다운로드"]').onclick=()=>{
      const a=document.createElement('a');
      a.href=URL.createObjectURL(new Blob(['%PDF-1.4\\nSYNTHETIC WORKER PROBE'],
                                         {type:'application/pdf'}));
      a.download='site-suggested.pdf';
      a.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
      URL.revokeObjectURL(a.href);
    };
    """
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({
        "download": {"default_directory": str(tmp_path / "downloads"),
                     "prompt_for_download": False, "directory_upgrade": True}
    }))
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium is not installed")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            downloads_path=str(tmp_path / "downloads"), accept_downloads=True,
        )
        try:
            def route(request):
                url = request.request.url
                if not url.startswith("https://www.saveticker.com/"):
                    request.abort()
                elif url.endswith("/probe.js"):
                    request.fulfill(content_type="application/javascript", body=script)
                else:
                    request.fulfill(
                        content_type="text/html; charset=utf-8", body=html,
                        headers={
                            "Content-Security-Policy": "default-src 'self'; script-src 'self'"
                        },
                    )

            context.route("**/*", route)
            page = context.new_page()
            # CDP allow/allowAndName bypasses Chrome filename determination and
            # ignores the extension API filename. Use the real download pipeline;
            # the temporary profile preference confines all files to tmp_path.
            context.new_cdp_session(page).send(
                "Browser.setDownloadBehavior",
                {"behavior": "default"},
            )
            page.goto(
                "https://www.saveticker.com/news#trader-saveticker=" + "b" * 32 + ":background",
                wait_until="load",
            )
            page.wait_for_selector('[aria-label="프로필 메뉴"]')
            worker = context.service_workers[0] if context.service_workers else (
                context.wait_for_event("serviceworker", timeout=15000)
            )
            deadline = time.monotonic() + 10
            while not worker.evaluate("()=>!!globalThis.probeReady"):
                assert time.monotonic() < deadline, "Probe wrapper failed to initialize"
                time.sleep(0.1)
            worker.evaluate("()=>{void runProbe();return true;}")
            deadline = time.monotonic() + 35
            while True:
                result = worker.evaluate("""async()=>({calls:nativeCalls,
                    commands:commandResults,filenameEvents,filenameRegistrations,
                    state:await chrome.storage.local.get(null),
                    downloads:await chrome.downloads.search({})})""")
                if any(call["type"] == "pdf" for call in result["calls"]):
                    break
                assert time.monotonic() < deadline, json.dumps(result, ensure_ascii=False)
                time.sleep(0.1)
            pdf_calls = [call for call in result["calls"] if call["type"] == "pdf"]
            assert result["filenameRegistrations"] == 0
            assert result["filenameEvents"] == []
            assert len(pdf_calls) == 1
            assert pdf_calls[0]["ticket"] == "synthetic-ticket"
            assert pdf_calls[0]["item_id"] == "synthetic-report"
            assert pdf_calls[0]["download_name"] == _NAME
            assert len(result["downloads"]) == 1
            item = result["downloads"][0]
            assert item["state"] == "complete"
            assert item["filename"].replace("\\", "/").endswith("/SaveTicker/" + _NAME)
            assert (tmp_path / "downloads" / "SaveTicker" / _NAME).read_bytes().startswith(b"%PDF-")
        finally:
            context.close()
