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


def test_scalp_job_flushes_queued_digest_when_tick_is_skipped(conn, project):
    """Codex 리뷰(T3): 묶음 알림이 outbox 에 남은 채 단타를 끄거나 창 밖·일시정지면 tick 이 돌지
    않아 영영 안 간다 → 잡이 건너뛸 때 그날 묶음만 보낸다."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine

    now = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)  # 금 23:00 KST — 창 밖
    deps = _deps(conn, FakeMarket(), now=lambda: now)
    sent = []
    deps.notifier = type(
        "N", (), {"send": lambda self, t, *, escape=True: sent.append(t) or True}
    )()
    day = run_date_for("KR", now)
    item = {"leg": "exit", "label": "005930", "price": "10300", "reason": "close", "ret": 0.01}
    engine.save_state(None, day, "KR", {"open": [], "outbox": [item]})
    job = next(
        j for j in daemon.build_jobs(SCHED, deps.settings, markets=["KR"]) if j.id == "kr_scalp"
    )
    assert "skipped" in daemon.job_body(deps, job)()
    assert len(sent) == 1 and "청산 005930 @10,300원 · 마감 청산" in sent[0]
    assert engine.load_state(None, day, "KR")["outbox"] == []
    daemon.job_body(deps, job)()
    assert len(sent) == 1  # 비운 뒤엔 다시 안 보낸다
