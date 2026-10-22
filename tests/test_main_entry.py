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


def test_main_traderapp_routes_to_tray(monkeypatch):
    """창 없는 실행파일(TraderApp)을 더블클릭하면 `app --tray` 로, 콘솔 trader 는 `app` 으로."""
    from trader import __main__ as entry
    from trader import cli

    calls: list[list[str] | None] = []
    monkeypatch.setattr(cli, "app", lambda args=None: calls.append(args))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["TraderApp"])
    monkeypatch.setattr(sys, "executable", "/x/dist/trader/TraderApp")
    entry.main()
    monkeypatch.setattr(sys, "executable", "/x/dist/trader/trader")
    entry.main()
    assert calls == [["app", "--tray"], ["app"]]


def test_force_utf8_and_pause_tolerate_missing_streams(monkeypatch):
    """console=False 실행파일은 stdout/stderr/stdin 이 None 이다."""
    from trader import __main__ as entry
    from trader import cli

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)
    cli._force_utf8()
    entry._pause_if_console()
