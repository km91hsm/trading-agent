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
