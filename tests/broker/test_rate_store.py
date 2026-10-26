"""교차 프로세스 레이트리미터(리뷰 F10): 파일 하나를 여러 리미터(=프로세스)가 나눠 쓴다."""

import pytest

from trader.broker.kis_client import RateLimitBusy
from trader.broker.rate_store import SharedRateLimiter


def _pair(tmp_path, rate=2):
    clock = {"t": 1000.0}
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    mk = lambda: SharedRateLimiter(  # noqa: E731
        rate, tmp_path / "kis_rate.db", "paper", clock=lambda: clock["t"], sleep=sleep
    )
    return mk(), mk(), slept, clock


def test_two_limiters_share_one_window(tmp_path):
    a, b, slept, clock = _pair(tmp_path, rate=2)
    a.acquire()
    b.acquire()
    assert slept == []
    b.acquire()  # 세 번째는 다른 리미터여도 1초 창이 찰 때까지 기다린다
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.0
    clock["t"] += 2
    a.acquire()
    assert len(slept) == 1


def test_envs_are_independent(tmp_path):
    a, _, slept, clock = _pair(tmp_path, rate=1)
    real = SharedRateLimiter(1, tmp_path / "kis_rate.db", "real", clock=lambda: clock["t"])
    a.acquire()
    real.acquire()
    assert slept == []


def test_max_wait_raises_busy(tmp_path):
    a, b, slept, clock = _pair(tmp_path, rate=1)
    a.acquire()
    with b.max_wait(0.5), pytest.raises(RateLimitBusy):
        b.acquire()
    assert slept == []


def test_connection_reused_per_thread(tmp_path, monkeypatch):
    """전체 검토 P13: 호출마다 SQLite 연결·DDL 을 다시 하지 않는다(스레드당 1개)."""
    import sqlite3
    import threading

    from trader.broker import rate_store

    made = []
    real = sqlite3.connect

    def spy(*a, **kw):
        made.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(rate_store.sqlite3, "connect", spy)
    lim = rate_store.SharedRateLimiter(100, tmp_path / "rate.db", "real")
    for _ in range(5):
        lim.acquire()
    assert len(made) == 1
    t = threading.Thread(target=lim.acquire)
    t.start()
    t.join()
    assert len(made) == 2 and made[0] != made[1]
