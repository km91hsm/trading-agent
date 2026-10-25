"""프로세스 간 파일 락 (platform/filelock)."""

import threading

import pytest

from trader.platform.filelock import LockTimeout, locked


def test_lock_is_exclusive_and_released(tmp_path):
    p = tmp_path / "x.lock"
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with locked(p):
            entered.set()
            release.wait(2)
            order.append("holder-out")

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(2)
    with pytest.raises(LockTimeout):
        with locked(p, timeout=0.2, poll_s=0.02):
            pass
    release.set()
    t.join(2)
    with locked(p, timeout=1):
        order.append("second-in")
    assert order == ["holder-out", "second-in"]
