"""번들 빌드: (웹 빌드) → PyInstaller → 스모크 → zip. 셸 스크립트 대신 파이썬 (CLAUDE.md 규칙).

사용: uv run --group build python packaging/build.py [--skip-web] [--skip-smoke]
산출: dist/trader/ (실행파일 + _internal/) 와 dist/trader-<os>-<arch>.zip
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
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


def assemble_dist(dist: Path, *, sys_platform: str = sys.platform) -> Path:
    """배포 폴더 `dist/trader/` 를 만든다.

    macOS: PyInstaller 가 만든 `dist/Trader.app` 을 `dist/trader/` 안으로 옮기고 터미널용 심링크
    `trader → Trader.app/Contents/MacOS/trader` 를 둔다 (COLLECT 가 남긴 평면 폴더는 지운다).
    Windows: `dist/trader/{trader.exe, TraderApp.exe, _internal/}` 그대로."""
    target = dist / "trader"
    if sys_platform != "darwin":
        return target
    app = dist / "Trader.app"
    if not app.is_dir():
        raise SystemExit(f"Trader.app 이 없습니다: {app}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.move(str(app), str(target / "Trader.app"))
    os.symlink(Path("Trader.app") / "Contents" / "MacOS" / "trader", target / "trader")
    return target


# 번들 폴더 = 운용 중인 TRADER_HOME 일 수 있다(Trader.app 의 부모). 빌드가 폴더를 비우기 전에
# 사용자 데이터를 옆으로 옮겼다가 되돌린다 — 원장·키·설정이 지워지던 문제(전체 검토 2026-10-27)
USER_DATA = ("config", "secrets", "state", "docs")


def stash_user_data(target: Path, stash_root: Path) -> Path | None:
    """target 안의 사용자 데이터 폴더를 stash_root/<시각> 으로 옮긴다. 없으면 None."""
    present = [n for n in USER_DATA if (target / n).exists()]
    if not present:
        return None
    from datetime import datetime

    stash = stash_root / f"trader-userdata-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    stash.mkdir(parents=True)
    for n in present:
        shutil.move(str(target / n), str(stash / n))
    print(f"사용자 데이터 보존: {', '.join(present)} → {stash}", flush=True)
    return stash


def restore_user_data(stash: Path | None, target: Path) -> None:
    """stash 의 폴더를 target 으로 되돌린다(새 번들에 같은 이름이 있으면 사용자 것이 이긴다)."""
    if stash is None:
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(stash.iterdir()):
        dst = target / item.name
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.move(str(item), str(dst))
    stash.rmdir()
    print(f"사용자 데이터 복원 → {target}", flush=True)


def cli_exe(dist_dir: Path) -> Path:
    return dist_dir / ("trader.exe" if sys.platform == "win32" else "trader")


def internal_dir(dist_dir: Path) -> Path:
    """리소스 폴더: Windows `_internal/`, macOS `.app` 의 Contents/Frameworks (sys._MEIPASS)."""
    if sys.platform == "darwin":
        return dist_dir / "Trader.app" / "Contents" / "Frameworks"
    return dist_dir / "_internal"


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
    assemble_dist(ROOT / "dist")
    exe = cli_exe(DIST)
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
    internal = internal_dir(DIST)
    for rel in (
        "agents/orchestrator.md", "templates/config/accounts.yaml", "web/dist/index.html",
        "icons/tray-dark.png",
    ):
        if not (internal / rel).exists():
            raise SystemExit(f"번들 리소스 누락: {internal.name}/{rel}")
    for bad in ("secrets", "state"):
        if (DIST / bad).exists() or (internal / bad).exists():
            raise SystemExit(f"번들에 {bad}/ 가 들어갔습니다")
    windowed = DIST / ("TraderApp.exe" if sys.platform == "win32" else "Trader.app/Contents/MacOS/TraderApp")
    if not windowed.exists():
        raise SystemExit(f"창 없는 실행파일이 없습니다: {windowed}")
    if sys.platform == "darwin" and not (DIST / "Trader.app" / "Contents" / "Info.plist").exists():
        raise SystemExit("Trader.app/Contents/Info.plist 가 없습니다")
    out["windowed"] = str(windowed)
    return out


PLATFORM_NAMES = {"darwin": "macos", "win32": "windows", "linux": "linux"}
ARCH_NAMES = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}


def project_version() -> str:
    """버전의 출처는 pyproject.toml 하나 (태그·zip 이름·Info.plist 가 같은 값을 쓴다)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("pyproject.toml 에 version 이 없습니다")
    return m.group(1)


def dist_name(version: str, sys_platform: str, machine: str) -> str:
    """zip 이름: trader-<버전>-<macos|windows|linux>-<arch>."""
    arch = machine.lower()
    arch = ARCH_NAMES.get(arch, arch)
    return f"trader-{version}-{PLATFORM_NAMES.get(sys_platform, sys_platform)}-{arch}"


def make_zip(dist_dir: Path, name: str, *, readme: Path | None = None) -> Path:
    """dist_dir 을 `trader/` 최상위로 압축한다. 심링크는 링크로 보존하고(macOS framework·CLI 링크가
    실제 파일로 풀리면 크기가 1.5배가 된다), README 가 있으면 최상위에 동봉한다."""
    if readme is not None:
        shutil.copy2(readme, dist_dir / "README.md")
    target = dist_dir.parent / f"{name}.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(dist_dir.rglob("*")):
            arc = (Path("trader") / p.relative_to(dist_dir)).as_posix()
            if p.is_symlink():
                zi = zipfile.ZipInfo(arc)
                zi.create_system = 3  # unix: external_attr 의 상위 16비트가 st_mode
                zi.external_attr = (stat.S_IFLNK | 0o755) << 16
                zf.writestr(zi, os.readlink(p))
            elif p.is_dir():
                continue
            else:
                zf.write(p, arc)
    return target


def main() -> None:
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true", help="웹 빌드 생략 (기존 web/dist 사용)")
    ap.add_argument("--skip-smoke", action="store_true")
    ap.add_argument("--check-tag", metavar="TAG", help="v<버전> 태그가 pyproject 버전과 같은지만 확인하고 끝낸다")
    args = ap.parse_args()
    if args.check_tag:
        want = f"v{project_version()}"
        if args.check_tag != want:
            raise SystemExit(f"태그 {args.check_tag} 가 pyproject 버전 {want} 과 다릅니다")
        print(want)
        return
    if not args.skip_web:
        build_web()
    # 빌드·스모크·압축이 모두 끝날 때까지 사용자 데이터를 밖에 둔다 — 스모크가 기존 secrets 로
    # 실패하거나 zip 에 원장·키가 들어가지 않게(Codex 리뷰). 실패해도 finally 에서 되돌린다
    stash = stash_user_data(DIST, ROOT / "dist")
    try:
        exe = build_bundle()
        result = {"exe": str(exe)}
        if not args.skip_smoke:
            result["smoke"] = smoke(exe)
        name = dist_name(project_version(), sys.platform, platform.machine())
        result["zip"] = str(make_zip(DIST, name, readme=ROOT / "docs" / "bundle-usage.md"))
        result["zip_name"] = f"{name}.zip"
    finally:
        restore_user_data(stash, DIST)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
