"""SQLite 원장: 연결, 스키마 버전·순서 마이그레이션, 계좌 동기화, 실행 기록."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from trader.config import AccountConfig
from trader.platform import paths

# 마이그레이션은 (버전, SQL) 순서 목록. 새 테이블·컬럼은 버전을 올려 뒤에 추가한다.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            env TEXT NOT NULL,
            cano TEXT NOT NULL,
            acnt_prdt_cd TEXT NOT NULL,
            markets TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS control (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            market TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_calls (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            ticker TEXT,
            market TEXT,
            agent TEXT NOT NULL,
            model TEXT,
            session_id TEXT,
            cost_usd REAL,
            duration_ms INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        );
        """,
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """원장 DB 연결을 연다. 기본 경로는 state_dir()/trader.db."""
    db_path = path if path is not None else paths.state_dir() / "trader.db"
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def init_schema(conn: sqlite3.Connection) -> None:
    """현재 버전보다 큰 마이그레이션만 순서대로 적용한다. 멱등."""
    version = current_version(conn)
    for target, sql in MIGRATIONS:
        if target <= version:
            continue
        conn.executescript(sql)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (target,))
        conn.commit()
        version = target


def sync_accounts(conn: sqlite3.Connection, accounts: list[AccountConfig]) -> None:
    """accounts.yaml 의 계좌를 id 기준으로 upsert 한다."""
    created = now_iso()
    for a in accounts:
        conn.execute(
            "INSERT INTO accounts(id, env, cano, acnt_prdt_cd, markets, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET env=excluded.env, cano=excluded.cano, "
            "acnt_prdt_cd=excluded.acnt_prdt_cd, markets=excluded.markets",
            (a.id, a.env, a.cano, a.acnt_prdt_cd, json.dumps(a.markets), created),
        )
    conn.commit()


def start_run(conn: sqlite3.Connection, kind: str, market: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(kind, market, started_at, status) VALUES (?, ?, ?, 'running')",
        (kind, market, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
        (now_iso(), status, error, run_id),
    )
    conn.commit()
