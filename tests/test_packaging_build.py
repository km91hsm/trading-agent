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
    code = "import sys; print('표준출력 단서'); print('표준오류 단서', file=sys.stderr); sys.exit(3)"
    with pytest.raises(subprocess.CalledProcessError):
        build._run([sys.executable, "-c", code], capture_output=True)
    out = capsys.readouterr()
    assert "표준출력 단서" in out.out and "표준오류 단서" in out.err
