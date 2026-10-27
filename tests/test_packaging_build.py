"""packaging/build.py 의 보조 함수: 실패한 하위 명령의 출력이 CI 로그에 남아야 원인을 볼 수 있다."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_build():
    path = Path(__file__).resolve().parents[1] / "packaging" / "build.py"
    spec = importlib.util.spec_from_file_location("trader_packaging_build", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_run_prints_captured_output_when_command_fails(capsys):
    """capture_output=True 로 돈 스모크(`trader.exe --help`)가 exit 1 이면 stdout/stderr 를
    출력해야 한다 — v0.1.2 Windows CI 는 원인 없이 CalledProcessError 만 남았다."""
    build = _load_build()
    # 자식 파이썬의 콘솔 인코딩(Windows cp949)에 걸리지 않게 ASCII 단서만 쓴다
    code = "import sys; print('OUT-CLUE'); print('ERR-CLUE', file=sys.stderr); sys.exit(3)"
    with pytest.raises(subprocess.CalledProcessError):
        build._run([sys.executable, "-c", code], capture_output=True)
    out = capsys.readouterr()
    assert "OUT-CLUE" in out.out and "ERR-CLUE" in out.err


def test_force_utf8_lets_korean_result_json_print(monkeypatch):
    """CI(Windows) 의 스크립트 stdout 은 cp1252 — 결과 JSON 의 한글(init 메시지)에서 죽었다."""
    import io
    import json

    build = _load_build()
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    build._force_utf8()
    print(json.dumps({"init": "생성: config/accounts.yaml"}, ensure_ascii=False))
    sys.stdout.flush()
    assert "생성" in raw.getvalue().decode("utf-8")


def test_dist_name_uses_version_and_friendly_platform():
    build = _load_build()
    assert build.dist_name("0.1.3", "darwin", "arm64") == "trader-0.1.3-macos-arm64"
    assert build.dist_name("0.1.3", "darwin", "x86_64") == "trader-0.1.3-macos-x86_64"
    assert build.dist_name("0.1.3", "win32", "AMD64") == "trader-0.1.3-windows-x86_64"
    assert build.dist_name("0.2.0", "linux", "aarch64") == "trader-0.2.0-linux-arm64"


def test_project_version_reads_pyproject():
    build = _load_build()
    v = build.project_version()
    assert v and v[0].isdigit()


def test_make_zip_preserves_symlinks_and_includes_readme(tmp_path):
    """zip 이 심링크를 실제 파일로 풀어 넣으면 268MB 가 396MB 가 된다 — 링크는 링크로."""
    import os
    import stat
    import zipfile

    build = _load_build()
    dist = tmp_path / "trader"
    (dist / "Trader.app" / "Contents" / "MacOS").mkdir(parents=True)
    real = dist / "Trader.app" / "Contents" / "MacOS" / "trader"
    real.write_text("bin", encoding="utf-8")
    os.symlink("Trader.app/Contents/MacOS/trader", dist / "trader")
    readme = tmp_path / "bundle-usage.md"
    readme.write_text("# 사용법\n", encoding="utf-8")
    out = build.make_zip(dist, "trader-0.1.3-macos-arm64", readme=readme)
    assert out.name == "trader-0.1.3-macos-arm64.zip"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "trader/README.md" in names
        info = zf.getinfo("trader/trader")
        assert stat.S_ISLNK(info.external_attr >> 16)
        assert zf.read("trader/trader").decode() == "Trader.app/Contents/MacOS/trader"
        assert zf.read("trader/Trader.app/Contents/MacOS/trader").decode() == "bin"


def test_assemble_dist_darwin_layout(tmp_path):
    """macOS: dist/Trader.app 을 dist/trader/ 안으로 옮기고 CLI 심링크를 둔다
    (COLLECT 폴더는 치운다)."""
    build = _load_build()
    dist = tmp_path / "dist"
    macos = dist / "Trader.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "trader").write_text("cli", encoding="utf-8")
    (macos / "TraderApp").write_text("app", encoding="utf-8")
    (dist / "trader" / "_internal").mkdir(parents=True)  # PyInstaller 의 COLLECT 산출물
    out = build.assemble_dist(dist, sys_platform="darwin")
    assert out == dist / "trader"
    assert (out / "Trader.app" / "Contents" / "MacOS" / "TraderApp").exists()
    link = out / "trader"
    assert link.is_symlink()
    assert link.resolve() == (out / "Trader.app" / "Contents" / "MacOS" / "trader").resolve()
    assert link.read_text(encoding="utf-8") == "cli"
    assert not (out / "_internal").exists()


def test_assemble_dist_windows_keeps_flat(tmp_path):
    build = _load_build()
    dist = tmp_path / "dist"
    (dist / "trader" / "_internal").mkdir(parents=True)
    (dist / "trader" / "trader.exe").write_text("", encoding="utf-8")
    (dist / "trader" / "TraderApp.exe").write_text("", encoding="utf-8")
    out = build.assemble_dist(dist, sys_platform="win32")
    assert out == dist / "trader"
    assert (out / "TraderApp.exe").exists() and (out / "_internal").exists()


def test_user_data_survives_rebuild(tmp_path):
    """번들 폴더가 운용 중인 TRADER_HOME 이어도 빌드가 config·secrets·state 를 지우지 않는다."""
    import shutil

    build = _load_build()
    target = tmp_path / "trader"
    (target / "state").mkdir(parents=True)
    (target / "state" / "trader.db").write_text("원장", encoding="utf-8")
    (target / "config").mkdir()
    (target / "Trader.app").mkdir()
    stash = build.stash_user_data(target, tmp_path)
    assert stash is not None and not (target / "state").exists()
    shutil.rmtree(target)  # PyInstaller·assemble_dist 가 폴더를 비운다
    (target / "Trader.app").mkdir(parents=True)
    build.restore_user_data(stash, target)
    assert (target / "state" / "trader.db").read_text(encoding="utf-8") == "원장"
    assert (target / "config").is_dir() and not stash.exists()
    assert build.stash_user_data(tmp_path / "없음", tmp_path) is None
