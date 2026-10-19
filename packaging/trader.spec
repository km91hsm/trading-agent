# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder 번들: dist/trader/{trader(.exe), _internal/}.

_internal/ 에 런타임·라이브러리와 리소스(agents/ web/dist templates/ docs/)가 들어가고, 실행파일 폴더가
TRADER_HOME 이다 (platform/bundle.py). secrets/·state/ 는 넣지 않는다."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # packaging/ → 저장소 루트

datas = [
    (str(ROOT / ".claude" / "agents"), "agents"),
    (str(ROOT / "templates"), "templates"),
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
    + ["pyarrow", "pyarrow.parquet", "tzdata", "youtube_transcript_api", "bs4"]
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
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="trader",
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="trader", contents_directory="_internal")
