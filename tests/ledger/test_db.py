import sqlite3

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
    assert {"accounts", "control", "runs", "agent_calls", "schema_version"} <= _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    assert db.current_version(conn) == db.SCHEMA_VERSION


def test_migration_applies_only_newer(conn, monkeypatch):
    extra = (db.SCHEMA_VERSION + 1, "CREATE TABLE IF NOT EXISTS extra_t (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, extra])
    monkeypatch.setattr(db, "SCHEMA_VERSION", extra[0])
    db.init_schema(conn)
    assert "extra_t" in _tables(conn)
    assert db.current_version(conn) == extra[0]
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


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
