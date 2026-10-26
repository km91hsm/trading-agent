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


def test_paper_review_job_weekly_message(conn, project):
    from conftest import make_account

    PAPER = make_account("paper-main", "paper", "auto", ["KR"])
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert jobs["paper_review"].cron == {"hour": 9, "minute": 0, "day_of_week": "sat"}
    deps = _deps(conn, None, now=lambda: NOW)
    deps.accounts = [PAPER]
    sent = []
    deps.notifier.send = lambda text, **kw: sent.append(text) or True
    assert daemon._paper_review_body(deps)["items"] == 1 and sent == []  # 주문 없으면 조용히
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, json,"
        " created_at) VALUES ('2026-10-26','KR','005930','BUY',0.1,0.7,'{}','2026-10-26T01:00:00Z')"
    )
    conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, price,"
        " status, mode, created_at) VALUES (1,'paper-main','005930','KR','BUY',10,'limit','10000',"
        "'expired','auto','2026-10-26T01:00:00+00:00')"
    )
    conn.commit()
    daemon._paper_review_body(deps)
    assert sent and sent[-1].startswith("[모의 체결 점검]")


def test_daily_report_jobs_per_market(conn, project):
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert jobs["kr_daily_report"].cron["timezone"] == "Asia/Seoul"
    assert jobs["us_daily_report"].cron["timezone"] == "America/New_York"
    deps = _deps(conn, None, now=lambda: NOW)
    sent = []
    deps.notifier.send = lambda text, **kw: sent.append(text) or True
    assert daemon._daily_report_body(deps, "KR") == {"items": 0} and sent == []
