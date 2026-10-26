"""데몬 ↔ Stock 교훈 1차: 원장 백업 잡, 킬스위치 자동 판정, 주간 모의 체결 점검."""

from datetime import UTC, datetime

from test_daemon import SCHED, _deps

from trader import daemon
from trader.config import Settings

NOW = datetime(2026, 10, 27, 8, 10, tzinfo=UTC)


def test_backup_job_registered_and_runs(conn, project):
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert jobs["ledger_backup"].cron == {"hour": 17, "minute": 10}
    deps = _deps(conn, None, now=lambda: NOW)
    deps.state = project / "state"
    out = daemon._backup_body(deps)
    assert out["file"].startswith("trader-20261027") and out["pruned"] == 0
    assert (project / "state" / "backups" / out["file"]).exists()
