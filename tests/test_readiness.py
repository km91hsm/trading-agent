"""실전 전환 준비도 점검 (스펙 §11 Phase 8): 항목·임계값·모드 불변."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import make_account
from helpers_memory import seed_matured
from typer.testing import CliRunner

from trader import readiness
from trader.cli import app
from trader.config import Settings
from trader.control import control
from trader.ledger import db, repo

TODAY = date(2026, 10, 17)


def _runs(conn, n_ok: int, n_err: int = 0, kind="analyze", model="claude-opus"):
    for i in range(n_ok + n_err):
        d = TODAY - timedelta(days=i + 1)
        started = datetime(d.year, d.month, d.day, 1, tzinfo=UTC).isoformat()
        status = "ok" if i < n_ok else "error"
        cur = conn.execute(
            "INSERT INTO runs(kind, market, started_at, finished_at, status) "
            "VALUES (?, 'KR', ?, ?, ?)",
            (kind, started, started, status),
        )
        conn.execute(
            "INSERT INTO agent_calls(run_id, agent, model, status, created_at) "
            "VALUES (?, 'orchestrator', ?, ?, ?)",
            (cur.lastrowid, model, status, started),
        )
    conn.commit()


def _navs(conn, values, account="paper-main"):
    for i, v in enumerate(values):
        repo.snapshot_positions(
            conn,
            account_id=account,
            d=TODAY - timedelta(days=len(values) - i),
            market="KR",
            nav=Decimal(str(v)),
            cash=Decimal(1),
            positions=[],
        )


def test_readiness_items_and_thresholds(project, conn):
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    r = readiness.check(
        conn, accounts=accounts, settings=Settings(), state=project / "state", today=TODAY
    )
    keys = {i["key"] for i in r["items"]}
    assert {
        "paper_days",
        "run_error_rate",
        "order_failed_rate",
        "return_vs_benchmark",
        "mdd",
        "calibration_active",
        "stale_approvals",
    } <= keys
    assert r["ok"] is False
    assert next(i for i in r["items"] if i["key"] == "paper_days")["value"] == 0

    _runs(conn, 22, n_err=1)
    _navs(conn, [100, 101, 102, 101, 103, 104])
    seed_matured(conn, 60)
    r = readiness.check(
        conn, accounts=accounts, settings=Settings(), state=project / "state", today=TODAY
    )
    by = {i["key"]: i for i in r["items"]}
    assert by["paper_days"]["ok"] and by["paper_days"]["value"] == 22
    assert by["run_error_rate"]["ok"] and by["run_error_rate"]["value"] < 0.1
    assert by["order_failed_rate"]["ok"]
    assert by["return_vs_benchmark"]["ok"]
    assert by["mdd"]["ok"] and by["mdd"]["value"] < 0
    assert by["calibration_active"]["value"] is True
    assert by["stale_approvals"]["ok"] and by["stale_approvals"]["value"] == 0
    assert r["ok"] is True

    # 만료된 미결 승인 → 불합격
    repo.create_approval(
        conn,
        decision_id=1,
        account_id="paper-main",
        order={},
        requested_at="2026-10-01T00:00:00+00:00",
        expires_at="2026-10-01T00:30:00+00:00",
    )
    r = readiness.check(
        conn, accounts=accounts, settings=Settings(), state=project / "state", today=TODAY
    )
    assert next(i for i in r["items"] if i["key"] == "stale_approvals")["ok"] is False
    assert r["ok"] is False


def test_readiness_never_changes_modes(project, conn):
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    _runs(conn, 25)
    _navs(conn, [100, 102, 104])
    seed_matured(conn, 60)
    r = readiness.check(
        conn, accounts=accounts, settings=Settings(), state=project / "state", today=TODAY
    )
    assert r["ok"] is True
    assert control.get_account_mode(conn, accounts[1]) == "off"
    assert conn.execute("SELECT COUNT(*) FROM control WHERE key LIKE 'mode:%'").fetchone()[0] == 0


def test_cli_backtest_and_readiness(project):
    conn = db.connect()
    db.init_schema(conn)
    seed_matured(conn, 5, start=date(2026, 3, 2), hit_rate=1.0)
    conn.close()
    r = CliRunner().invoke(app, ["backtest", "--since", "2026-03-01"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["n"] == 5 and out["hit_rate"] == 1.0
    r = CliRunner().invoke(app, ["readiness"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"] is False and any(i["key"] == "paper_days" for i in out["items"])


def test_paper_days_excludes_fake_sessions(project, conn):
    accounts = [make_account("paper-main", "paper", "auto")]
    _runs(conn, 25, model="fake")
    r = readiness.check(
        conn, accounts=accounts, settings=Settings(), state=project / "state", today=TODAY
    )
    assert next(i for i in r["items"] if i["key"] == "paper_days")["value"] == 0


def test_stale_approvals_ignores_unexpired(project, conn):
    accounts = [make_account("paper-main", "paper", "auto")]
    far = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()  # 오늘 안에 만료되지만 아직 유효
    repo.create_approval(
        conn, decision_id=1, account_id="paper-main", order={}, requested_at=far, expires_at=far
    )
    r = readiness.check(
        conn,
        accounts=accounts,
        settings=Settings(),
        state=project / "state",
        today=datetime.now(UTC).date(),
    )
    item = next(i for i in r["items"] if i["key"] == "stale_approvals")
    assert item["ok"] and item["value"] == 0


def test_run_error_rate_ignores_interval_jobs(conn, project):
    _runs(conn, 9, 1)  # cron 잡(analyze): ok 9 + error 1
    _runs(conn, 0, 5, kind="monitor")  # interval 잡은 오류일 때만 기록 → 분모에서 제외
    r = readiness.check(
        conn, accounts=[], settings=Settings(), state=project / "state", today=TODAY
    )
    item = next(i for i in r["items"] if i["key"] == "run_error_rate")
    assert item["value"] == pytest.approx(0.1)
