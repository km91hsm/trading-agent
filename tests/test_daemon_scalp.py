"""데몬 ↔ 단타(Phase 23 S): 잡 등록·꺼져 있으면 skipped·창 밖 skipped·켜면 engine 틱."""

from datetime import UTC, datetime

from test_daemon import SCHED, _deps

from trader import daemon
from trader.broker.fake import FakeMarket
from trader.config import ScalpCfg, Settings


def test_build_jobs_registers_scalp_interval_per_market():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert jobs["kr_scalp"].kind == "scalp" and jobs["kr_scalp"].interval_s == 60
    assert jobs["us_scalp"].market == "US"


def test_scalp_body_skipped_when_disabled_or_outside_window(conn, project):
    now = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # 10:00 KST
    deps = _deps(conn, FakeMarket(), now=lambda: now)
    assert daemon._scalp_body(deps, "KR") == {"skipped": "scalp.enabled=false"}
    deps.settings = Settings(scalp=ScalpCfg(enabled=True))
    out = daemon._scalp_body(deps, "KR")
    assert out["entries"] == [] and out["candidates"] == 0  # _scan 없음 → 후보 0
    night = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
    deps.now = lambda: night
    job = next(
        j for j in daemon.build_jobs(SCHED, deps.settings, markets=["KR"]) if j.id == "kr_scalp"
    )
    assert "skipped" in daemon.job_body(deps, job)()
