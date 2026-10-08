"""런타임 제어 플래그와 계좌 모드 (SQLite control 테이블, JSON 값)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, get_args

from trader.config import AccountConfig, Mode
from trader.ledger.db import now_iso

LIVE_CONFIRM = "LIVE"


class ConfirmationRequired(Exception):
    """실전 계좌를 auto 로 전환하려는데 확인 문자열이 없을 때 발생."""


def get_flag(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def set_flag(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO control(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )
    conn.commit()


def is_paused(conn: sqlite3.Connection) -> bool:
    return bool(get_flag(conn, "paused", False))


def get_account_mode(conn: sqlite3.Connection, account: AccountConfig) -> Mode:
    """control 테이블 값이 있으면 그것을, 없으면 yaml 모드를 돌려준다."""
    return get_flag(conn, f"mode:{account.id}", account.mode)


def set_account_mode(
    conn: sqlite3.Connection,
    account: AccountConfig,
    mode: str,
    *,
    confirm: str | None = None,
) -> None:
    if mode not in get_args(Mode):
        raise ValueError(f"알 수 없는 모드: {mode}")
    if account.env == "real" and mode == "auto" and confirm != LIVE_CONFIRM:
        raise ConfirmationRequired(
            f"실전 계좌 '{account.id}' 를 auto 로 바꾸려면 confirm='{LIVE_CONFIRM}' 가 필요합니다."
        )
    set_flag(conn, f"mode:{account.id}", mode)
