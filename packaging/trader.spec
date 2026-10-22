# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder 번들: 실행파일 둘 + _internal/.

- `trader`(console=True): CLI·세션의 `trader tool`·launchd 의 `trader daemon`.
- `TraderApp`(console=False): 더블클릭용 — 트레이/메뉴바 아이콘 + 데몬 + 웹 (Windows 는 대소문자를 무시하므로
  `Trader` 가 아니라 `TraderApp`).
- macOS 는 BUNDLE 로 `Trader.app` 을 만든다. TRADER_HOME 은 `.app` 의 부모 폴더 (platform/bundle.py).
_internal/ 에 런타임·라이브러리와 리소스(agents/ web/dist templates/ docs/ icons/)가 들어간다. secrets/·state/ 는
넣지 않는다."""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # packaging/ → 저장소 루트
VERSION = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
ICON = str(ROOT / "packaging" / "icons" / ("trader.icns" if sys.platform == "darwin" else "trader.ico"))

datas = [
    (str(ROOT / ".claude" / "agents"), "agents"),
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "packaging" / "icons"), "icons"),
]
for rel, dst in (("web/dist", "web/dist"), ("docs/runbook-live-transition.md", "docs"),
                 ("docs/bundle-usage.md", "docs")):
    if (ROOT / rel).exists():
        datas.append((str(ROOT / rel), dst))
datas += collect_data_files("tzdata")

hiddenimports = (
    collect_submodules("trader")
    + collect_submodules("sklearn")
    + collect_submodules("uvicorn")
    + collect_submodules("apscheduler")
    + ["pyarrow", "pyarrow.parquet", "tzdata", "youtube_transcript_api", "bs4", "PIL.Image", "PIL.PngImagePlugin"]
    + (["pystray._darwin"] if sys.platform == "darwin" else ["pystray._win32"] if sys.platform == "win32" else [])
)

a = Analysis(
    [str(ROOT / "src" / "trader" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["lightgbm", "torch", "stable_baselines3", "gymnasium", "matplotlib", "tkinter", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe_cli = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="trader",
    console=True,
    icon=ICON,
    disable_windowed_traceback=False,
)
exe_app = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="TraderApp",
    console=False,
    icon=ICON,
    disable_windowed_traceback=True,
)
coll = COLLECT(exe_cli, exe_app, a.binaries, a.datas, name="trader", contents_directory="_internal")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Trader.app",
        icon=ICON,
        bundle_identifier="dev.trader.app",
        info_plist={
            "CFBundleName": "Trader",
            "CFBundleDisplayName": "Trader",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleExecutable": "TraderApp",
            "LSUIElement": True,  # Dock 아이콘 없이 메뉴바만
            "NSHighResolutionCapable": True,
        },
    )
