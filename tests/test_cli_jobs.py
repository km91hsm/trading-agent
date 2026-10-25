"""`trader jobs list|run|kinds` (Phase 21): 요청만 넣고 실행은 데몬."""

import json
import os
from datetime import UTC, datetime

from typer.testing import CliRunner

from trader import heartbeat, jobs
from trader.cli import app
from trader.ledger import db


def _daemon_alive(project):
    (project / "state" / heartbeat.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    hb = heartbeat.Heartbeat(project / "state" / heartbeat.FILE_NAME)
    hb.start(pid=os.getpid(), started_at=datetime.now(UTC), jobs=[])


def test_jobs_run_requires_daemon_then_enqueues(project):
    runner = CliRunner()
    r = runner.invoke(app, ["jobs", "run", "scan_analyze", "--market", "kr"])
    assert r.exit_code == 1 and "데몬이 꺼져" in (r.stdout + (r.stderr or ""))
    _daemon_alive(project)
    r = runner.invoke(app, ["jobs", "run", "scan_analyze", "--market", "kr"])
    assert r.exit_code == 0 and "KR 스캔 → 분석" in r.stdout, r.stdout
    r = runner.invoke(app, ["jobs", "run", "scan_analyze", "--market", "KR"])
    assert r.exit_code == 1 and "이미 대기" in (r.stdout + (r.stderr or ""))
    r = runner.invoke(app, ["jobs", "run", "bogus"])
    assert r.exit_code == 1 and "알 수 없는 작업" in (r.stdout + (r.stderr or ""))
    r = runner.invoke(
        app,
        [
            "jobs",
            "run",
            "analyze_ticker",
            "--market",
            "US",
            "--ticker",
            "aapl",
            "--exchange",
            "nas",
            "--live",
            "--priority",
            "3",
        ],
    )
    assert r.exit_code == 0, r.stdout
    conn = db.connect()
    rows = jobs.list_recent(conn)
    conn.close()
    assert [(x["kind"], x["priority"], x["source"]) for x in rows] == [
        ("analyze_ticker", 3, "cli"),
        ("scan_analyze", 0, "cli"),
    ]
    assert rows[0]["params"] == {
        "market": "US",
        "ticker": "AAPL",
        "exchange": "NAS",
        "dry_run": False,
    }
    r = runner.invoke(app, ["jobs", "list"])
    assert r.exit_code == 0 and "#2 queued  p3" in r.stdout and "AAPL" in r.stdout
    r = runner.invoke(app, ["jobs", "kinds"])
    assert r.exit_code == 0 and "reevaluate" in r.stdout and "(가벼움)" in r.stdout


def test_jobs_list_empty(project):
    r = CliRunner().invoke(app, ["jobs", "list"])
    assert r.exit_code == 0 and "요청 없음" in r.stdout
