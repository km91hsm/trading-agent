"""창 없는 실행파일·Windows 환경 차이(전체 검토 2026-10-27): stdout 이 None 이어도 웹 서버 설정이
죽지 않고, 진입점이 스트림을 채우며, Windows 세션 정리는 프로세스 트리째 끝낸다."""

import socket
import sys

from trader import daemon
from trader.orchestrator import session


def test_start_web_survives_none_stdout(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    t = daemon._start_web(FastAPI(), "127.0.0.1", port)  # 예전엔 Config 에서 ValueError
    assert t.is_alive() or t.ident is not None


def test_ensure_streams_replaces_none(monkeypatch):
    from trader import __main__ as entry

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    entry._ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("ok")  # 죽지 않는다


class _Proc:
    pid = 4242

    def __init__(self, finishes: bool):
        self.finishes, self.terminated = finishes, False
        self.stdin = self.stdout = self.stderr = None

    def terminate(self):
        self.terminated = True

    def communicate(self, timeout=None):
        import subprocess

        if not self.finishes:
            raise subprocess.TimeoutExpired("claude", timeout)
        return "", ""


def test_windows_terminate_kills_whole_tree(monkeypatch):
    from trader.platform import procs

    killed = []
    monkeypatch.setattr(session.os, "name", "nt")
    monkeypatch.setattr(procs, "kill_group", killed.append)
    monkeypatch.setattr(session, "TERM_GRACE_S", 0)
    monkeypatch.setattr(session, "KILL_WAIT_S", 0)
    p = _Proc(finishes=False)
    session.terminate_group(p)  # 예전엔 signal.SIGKILL(윈도우에 없음)에서 AttributeError
    assert p.terminated and killed == [4242]
    q = _Proc(finishes=True)
    session.terminate_group(q)
    assert killed == [4242, 4242]  # 부모가 끝나도 남은 손자를 정리


def test_translocated_path_detected():
    from trader import __main__ as entry

    p = "/private/var/folders/x/T/AppTranslocation/ABC/d/Trader.app/Contents/MacOS/TraderApp"
    assert entry.translocated(p)
    assert not entry.translocated("/Users/me/trader/Trader.app/Contents/MacOS/TraderApp")
