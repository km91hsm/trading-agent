import os
import sys
import threading

import pytest


def test_worker_failure_stops_other_lanes_and_preserves_exit_status(tmp_path):
    from trader.sources.saveticker.supervisor import WorkerExited, supervise

    snapshots = []
    commands = {
        "news": [sys.executable, "-c", "import time; time.sleep(30)"],
        "background": [sys.executable, "-c", "raise SystemExit(2)"],
    }
    with pytest.raises(WorkerExited):
        supervise(commands, tmp_path, threading.Event(), snapshots.append)
    assert snapshots
    workers = snapshots[0]["workers"]
    assert set(workers) == {"news", "background"}
    assert snapshots[-1]["workers"]["background"]["exit_code"] == 2
    for worker in workers.values():
        with pytest.raises(ProcessLookupError):
            os.kill(worker["pid"], 0)
