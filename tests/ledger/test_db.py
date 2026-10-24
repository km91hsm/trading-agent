import sqlite3

import pytest
from conftest import make_account

from trader.ledger import db


def _tables(c: sqlite3.Connection) -> set[str]:
    rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_connect_uses_wal_and_row_factory(project):
    c = db.connect()
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.row_factory is sqlite3.Row
    assert (project / "state" / "trader.db").exists()


def test_init_schema_idempotent(conn):
    db.init_schema(conn)
    expected = {"accounts", "control", "runs", "agent_calls", "schema_version", "job_requests"}
    assert expected <= _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    assert db.current_version(conn) == db.SCHEMA_VERSION


def test_migration_applies_only_newer(conn, monkeypatch):
    extra = (db.SCHEMA_VERSION + 1, ["CREATE TABLE IF NOT EXISTS extra_t (id INTEGER PRIMARY KEY)"])
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, extra])
    monkeypatch.setattr(db, "SCHEMA_VERSION", extra[0])
    db.init_schema(conn)
    assert "extra_t" in _tables(conn)
    assert db.current_version(conn) == extra[0]
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_migration_partial_failure_rolls_back(conn, monkeypatch):
    bad = (db.SCHEMA_VERSION + 1, ["ALTER TABLE runs ADD COLUMN note TEXT", "CREATE TABLE nope (;"])
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, bad])
    monkeypatch.setattr(db, "SCHEMA_VERSION", bad[0])
    with pytest.raises(sqlite3.OperationalError):
        db.init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "note" not in cols
    assert db.current_version(conn) == bad[0] - 1


def test_migration_does_not_reapply_non_idempotent_v1(conn, monkeypatch):
    # v1 을 IF NOT EXISTS 없는 문장으로 바꿔 두면 재적용 시 즉시 실패해야 한다.
    monkeypatch.setattr(db, "MIGRATIONS", [(1, ["CREATE TABLE control (key TEXT PRIMARY KEY)"])])
    db.init_schema(conn)  # 이미 v1 이상이므로 아무것도 적용하지 않는다
    assert db.current_version(conn) == db.SCHEMA_VERSION


def test_migrations_must_be_ascending():
    with pytest.raises(ValueError):
        db.check_migrations([(1, ["SELECT 1"]), (3, ["SELECT 1"]), (2, ["SELECT 1"])])


def test_sync_accounts_upsert(conn):
    db.sync_accounts(conn, [make_account("a", "paper", "auto")])
    db.sync_accounts(conn, [make_account("a", "paper", "auto", ["KR", "US"])])
    rows = conn.execute("SELECT id, markets FROM accounts").fetchall()
    assert len(rows) == 1 and "US" in rows[0]["markets"]


def test_run_lifecycle(conn):
    rid = db.start_run(conn, "analyze", "KR")
    row = conn.execute("SELECT status, started_at FROM runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "running" and row["started_at"].endswith("+00:00")
    db.finish_run(conn, rid, "error", "boom")
    row = conn.execute("SELECT status, error, finished_at FROM runs WHERE id=?", (rid,)).fetchone()
    assert (row["status"], row["error"]) == ("error", "boom") and row["finished_at"]


def test_v7_creates_rl_shadow(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rl_shadow)")}
    assert {
        "decision_id",
        "account_id",
        "model_version",
        "obs",
        "llm_weight",
        "proposed_weight",
        "applied",
    } <= cols
    sql = (
        "INSERT INTO rl_shadow(decision_id, account_id, created_at, model_version, obs, "
        "llm_weight, proposed_weight) VALUES (1,'paper-main','t','v','[]',0.1,0.2)"
    )
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_v8_adds_query_indexes(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "ix_orders_account_created",
        "ix_orders_decision_account",
        "ix_orders_market_ticker",
        "ix_risk_checks_decision",
        "ix_approvals_status",
        "ix_triggers_market_ticker",
        "ix_agent_calls_created",
        "ix_decisions_market_ticker",
        "ix_positions_account_market_date",
        "ix_fills_order",
    } <= names
