"""SQLite 원장: 연결, 스키마 버전·순서 마이그레이션, 계좌 동기화, 실행 기록."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from trader.config import AccountConfig
from trader.platform import paths

# 마이그레이션은 (버전, 문장 목록) 오름차순 목록. 새 테이블·컬럼은 버전을 올려 뒤에 추가한다.
# 한 버전의 문장들과 버전 갱신은 BEGIN IMMEDIATE 트랜잭션 하나로 묶인다(전부 적용 또는 전부 롤백).
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                env TEXT NOT NULL,
                cano TEXT NOT NULL,
                acnt_prdt_cd TEXT NOT NULL,
                markets TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS control (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                market TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                error TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS agent_calls (
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
            )""",
        ],
    ),
    (
        2,  # Phase 4: 결정·판정·주문·체결·포지션·승인 (스펙 §6.3)
        [
            """CREATE TABLE IF NOT EXISTS analyst_reports (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
                role TEXT NOT NULL, signal TEXT, score REAL, confidence REAL,
                json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(date, market, ticker, role)
            )""",
            """CREATE TABLE IF NOT EXISTS debate_turns (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
                side TEXT NOT NULL, round INTEGER NOT NULL, stance TEXT, conviction REAL,
                json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(date, market, ticker, side, round)
            )""",
            """CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
                action TEXT NOT NULL, target_weight REAL, confidence REAL, session_id TEXT,
                json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(date, market, ticker)
            )""",
            """CREATE TABLE IF NOT EXISTS risk_checks (
                id INTEGER PRIMARY KEY,
                decision_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                approved INTEGER NOT NULL, adjusted_qty INTEGER, reasons TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                decision_id INTEGER, account_id TEXT NOT NULL, ticker TEXT NOT NULL,
                market TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
                order_type TEXT NOT NULL, price TEXT, status TEXT NOT NULL, order_no TEXT,
                message TEXT, mode TEXT NOT NULL, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                qty INTEGER NOT NULL, price TEXT NOT NULL, filled_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS positions_snapshot (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL,
                market TEXT, qty INTEGER NOT NULL, avg_price TEXT, current_price TEXT,
                eval_amount TEXT, nav TEXT, cash TEXT, created_at TEXT NOT NULL,
                UNIQUE(account_id, date, ticker)
            )""",
            """CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY,
                decision_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                order_json TEXT NOT NULL, status TEXT NOT NULL,
                requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT
            )""",
        ],
    ),
    (
        3,  # Phase 4 리뷰: 시장가 주문 금액(amount), KR 취소용 org_no, _CASH 스냅샷의 market
        [
            "ALTER TABLE orders ADD COLUMN amount TEXT",
            "ALTER TABLE orders ADD COLUMN org_no TEXT",
        ],
    ),
    (
        4,  # Phase 6: 트리거 기록, 체결 중복 방지 인덱스
        [
            """CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY,
                account_id TEXT, market TEXT NOT NULL, ticker TEXT NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL, price TEXT,
                decision_id INTEGER, created_at TEXT NOT NULL,
                handled_at TEXT, session_id TEXT
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_dedupe "
            "ON fills(account_id, order_id, filled_at, qty, price)",
        ],
    ),
    (
        5,  # Phase 8: 메모리 엔트리, 보정 실행 기록
        [
            """CREATE TABLE IF NOT EXISTS memory_entries (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL, market TEXT NOT NULL, account_id TEXT,
                decision_id INTEGER UNIQUE, decided_at TEXT NOT NULL,
                situation_vector TEXT NOT NULL, situation_summary TEXT,
                decision TEXT NOT NULL, horizon_days INTEGER NOT NULL,
                realized_return REAL, alpha_vs_benchmark REAL, reflection TEXT,
                outcome_at TEXT, tier TEXT NOT NULL, importance REAL NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS calibration_runs (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                n_decisions INTEGER NOT NULL, active INTEGER NOT NULL, json TEXT NOT NULL
            )""",
        ],
    ),
    (
        6,  # Phase 8 리뷰: 원시 가격수익(HOLD 도 신호 채점 가능)
        ["ALTER TABLE memory_entries ADD COLUMN price_return REAL"],
    ),
    (
        7,  # Phase 9: RL 사이저 섀도 기록 (모의 계좌 한정, 결정·계좌당 1행)
        [
            """CREATE TABLE IF NOT EXISTS rl_shadow (
                id INTEGER PRIMARY KEY,
                decision_id INTEGER NOT NULL, account_id TEXT NOT NULL,
                created_at TEXT NOT NULL, model_version TEXT NOT NULL,
                obs TEXT NOT NULL, llm_weight REAL NOT NULL, proposed_weight REAL NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                UNIQUE(decision_id, account_id)
            )""",
        ],
    ),
    (
        8,  # 리뷰(성능): 리스크 규칙·모니터·웹 조회가 매번 거르는 열에 인덱스 (스키마 변경 없음)
        [
            "CREATE INDEX IF NOT EXISTS ix_orders_account_created "
            "ON orders(account_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_orders_decision_account "
            "ON orders(decision_id, account_id)",
            "CREATE INDEX IF NOT EXISTS ix_orders_market_ticker ON orders(market, ticker)",
            "CREATE INDEX IF NOT EXISTS ix_risk_checks_decision ON risk_checks(decision_id)",
            "CREATE INDEX IF NOT EXISTS ix_risk_checks_account ON risk_checks(account_id, id)",
            "CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status)",
            "CREATE INDEX IF NOT EXISTS ix_approvals_account ON approvals(account_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_triggers_market_ticker ON triggers(market, ticker)",
            "CREATE INDEX IF NOT EXISTS ix_agent_calls_created ON agent_calls(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_decisions_market_ticker "
            "ON decisions(market, ticker, date)",
            "CREATE INDEX IF NOT EXISTS ix_positions_account_market_date "
            "ON positions_snapshot(account_id, market, date)",
            "CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(order_id)",
            "CREATE INDEX IF NOT EXISTS ix_memory_outcome ON memory_entries(outcome_at, market)",
        ],
    ),
    (
        9,  # 세션 턴 수 (비용·실패 원인 분석 단서). 세션 1회 = agent_calls 1행
        ["ALTER TABLE agent_calls ADD COLUMN num_turns INTEGER"],
    ),
]


def check_migrations(migrations: list[tuple[int, list[str]]]) -> None:
    """버전이 양수이고 엄격히 오름차순인지 검사한다."""
    versions = [v for v, _ in migrations]
    if not versions or versions[0] < 1:
        raise ValueError("마이그레이션 버전은 1 이상이어야 합니다")
    if any(a >= b for a, b in pairwise(versions)):
        raise ValueError(f"마이그레이션 버전이 오름차순이 아닙니다: {versions}")


check_migrations(MIGRATIONS)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """원장 DB 연결을 연다. 기본 경로는 state_dir()/trader.db.

    check_same_thread=False 는 Phase 5 웹 서버(스레드 풀)가 같은 연결 팩토리를 쓰기 위한 것이다.
    """
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
    """현재 버전보다 큰 마이그레이션만 순서대로 적용한다. 멱등이며 동시 실행에 안전하다."""
    check_migrations(MIGRATIONS)
    version = current_version(conn)  # schema_version 테이블 보장
    conn.commit()
    if version >= SCHEMA_VERSION:
        # 최신이면 읽기만으로 끝낸다. 버전마다 BEGIN IMMEDIATE(쓰기 잠금)를 잡으면 tool 프로세스가
        # 데몬의 긴 쓰기 트랜잭션에 막힌다.
        return
    for target, statements in MIGRATIONS:
        conn.execute("BEGIN IMMEDIATE")  # 쓰기 잠금 안에서 버전을 다시 읽는다
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if target <= int(row["v"] or 0):
                conn.execute("COMMIT")
                continue
            for sql in statements:
                conn.execute(sql)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (target,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
