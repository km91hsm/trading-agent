"""`python -m trader` / 번들 진입점: 어떤 콘솔 인코딩이라도 한글 도움말이 깨지지 않아야 한다."""

import io
import sys

import pytest


def test_main_forces_utf8_before_dispatch(monkeypatch):
    """Windows 는 파이프로 출력되면 stdout 이 cp1252 라 typer/rich 도움말의 한글에서
    UnicodeEncodeError 로 죽었다 (v0.1.2 번들 스모크 `trader.exe --help`)."""
    from trader import __main__ as entry

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.setattr(sys, "argv", ["trader", "--help"])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code in (0, None)
    sys.stdout.flush()
    text = raw.getvalue().decode("utf-8")
    assert "Usage" in text and "KIS" in text  # 한글 포함 도움말이 UTF-8 로 쓰였다
