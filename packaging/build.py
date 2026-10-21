"""번들 빌드: (웹 빌드) → PyInstaller → 스모크 → zip. 셸 스크립트 대신 파이썬 (CLAUDE.md 규칙).

사용: uv run --group build python packaging/build.py [--skip-web] [--skip-smoke]
산출: dist/trader/ (실행파일 + _internal/) 와 dist/trader-<os>-<arch>.zip
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "trader"


def _force_utf8() -> None:
    """스크립트 자신의 stdout/stderr 를 UTF-8 로 (Windows CI 는 cp1252 라 결과 JSON 의 한글에서 죽는다)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    try:
        return subprocess.run(
            cmd, shell=False, check=True, encoding="utf-8", errors="replace", **kw
        )
    except subprocess.CalledProcessError as e:
        # capture_output 으로 돈 명령이 실패하면 출력이 묻힌다 → CI 로그에 꼬리를 남긴다
        if e.stdout:
            print(e.stdout[-4000:], flush=True)
        if e.stderr:
            print(e.stderr[-4000:], file=sys.stderr, flush=True)
        raise


def build_web() -> None:
    web = ROOT / "web"
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit("npm 이 없어 웹 프런트를 빌드할 수 없습니다 (--skip-web 로 기존 dist 사용)")
    if not (web / "node_modules").exists():
        _run([npm, "ci"], cwd=str(web))
    _run([npm, "run", "build"], cwd=str(web))


def build_bundle() -> Path:
    if not (ROOT / "web" / "dist" / "index.html").exists():
        raise SystemExit("web/dist 가 없습니다. 먼저 웹을 빌드하세요 (npm run build)")
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(ROOT / "packaging" / "trader.spec"),
            "--noconfirm",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(ROOT / "build" / "pyinstaller"),
        ],
        cwd=str(ROOT),
    )
    exe = DIST / ("trader.exe" if sys.platform == "win32" else "trader")
    if not exe.exists():
        raise SystemExit(f"실행파일이 없습니다: {exe}")
    return exe


def smoke(exe: Path) -> dict:
    """번들이 pyproject 없는 폴더에서 init·status 를 돌릴 수 있는지, 리소스가 들어갔는지 확인."""
    out: dict = {}
    _run([str(exe), "--help"], capture_output=True)
    with tempfile.TemporaryDirectory(prefix="trader-smoke-") as tmp:
        env = {**os.environ, "TRADER_HOME": tmp, "PYTHONIOENCODING": "utf-8"}
        r = _run([str(exe), "init"], capture_output=True, env=env)
        out["init"] = r.stdout.strip()[:200]
        for rel in ("config/accounts.yaml", "config/settings.yaml", "secrets/.env.example"):
            if not (Path(tmp) / rel).exists():
                raise SystemExit(f"init 뒤 {rel} 이 없습니다")
        if "00000000" not in (Path(tmp) / "config" / "accounts.yaml").read_text(encoding="utf-8"):
            raise SystemExit("accounts.yaml 템플릿에 계좌번호 자리(00000000)가 없습니다")
        r = _run([str(exe), "status"], capture_output=True, env=env)
        out["status"] = r.stdout.strip()[-120:]
        # 무거운 스택(sklearn·pyarrow·pandas·fastapi·uvicorn)이 실제로 import 되는지 — --help·status 는
        # 이들을 건드리지 않아 hidden import·dylib 누락을 못 잡는다
        r = _run([str(exe), "selfcheck"], capture_output=True, env=env)
        out["selfcheck"] = r.stdout.strip().splitlines()[-1][:200]
        _run([str(exe), "ml", "models"], capture_output=True, env=env)
        _run([str(exe), "rl", "models"], capture_output=True, env=env)
        if not (Path(tmp) / "state" / "STATUS.md").exists():
            raise SystemExit("status 가 STATUS.md 를 만들지 않았습니다")
    internal = DIST / "_internal"
    for rel in ("agents/orchestrator.md", "templates/config/accounts.yaml", "web/dist/index.html"):
        if not (internal / rel).exists():
            raise SystemExit(f"번들 리소스 누락: _internal/{rel}")
    for bad in ("secrets", "state"):
        if (DIST / bad).exists() or (internal / bad).exists():
            raise SystemExit(f"번들에 {bad}/ 가 들어갔습니다")
    return out


def make_zip() -> Path:
    name = f"trader-{sys.platform}-{platform.machine().lower()}"
    target = ROOT / "dist" / f"{name}.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(DIST.rglob("*")):
            zf.write(p, Path("trader") / p.relative_to(DIST))
    return target


def main() -> None:
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true", help="웹 빌드 생략 (기존 web/dist 사용)")
    ap.add_argument("--skip-smoke", action="store_true")
    args = ap.parse_args()
    if not args.skip_web:
        build_web()
    exe = build_bundle()
    result = {"exe": str(exe)}
    if not args.skip_smoke:
        result["smoke"] = smoke(exe)
    result["zip"] = str(make_zip())
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
