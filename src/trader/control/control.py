"""런타임 제어 플래그와 계좌 모드 (SQLite control 테이블, JSON 값)."""

from __future__ import annotations

import json
import re
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


_TICKER_RE = re.compile(r"^(?:(?:KR|US):)?[A-Z0-9.\-]{1,12}$")  # 선택적 시장 접두사
_KR_CODE_RE = re.compile(r"^\d{6}$")


def _split_market(item: str) -> tuple[str, str]:
    """`KR:005930` → ("KR", "005930"); 접두사가 없으면 6자리 숫자는 KR, 그 외 US."""
    if ":" in item:
        m, t = item.split(":", 1)
        return m, t
    return ("KR" if _KR_CODE_RE.match(item) else "US"), item


def get_watchlist(conn: sqlite3.Connection, market: str | None = None) -> list[str]:
    """런타임 관심종목(`/watch`, 웹). universe.watch 와 합쳐 scan 후보·분석 대상에 들어간다.

    market 을 주면 그 시장 종목만 접두사 없이 돌려준다(접두사 없는 항목은 코드 형식으로 추정).
    """
    v = get_flag(conn, "watchlist", [])
    items = [str(t) for t in v] if isinstance(v, list) else []
    if market is None:
        return items
    return [t for m, t in (_split_market(i) for i in items) if m == market]


def set_watchlist(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    """형식 검증(영숫자·.·- 12자 이내, 선택적 `KR:`/`US:` 접두사, 대문자화) 후 저장.

    같은 (시장, 종목) 은 처음 쓴 표기 하나만 남긴다(`005930` 과 `KR:005930` 은 같은 항목).
    """
    out: list[str] = []
    keys: set[tuple[str, str]] = set()
    for t in tickers:
        u = str(t).strip().upper()
        if not _TICKER_RE.match(u):
            raise ValueError(f"잘못된 종목코드: {t}")
        key = _split_market(u)
        if key not in keys:
            keys.add(key)
            out.append(u)
    set_flag(conn, "watchlist", out)
    return out


def remove_watch(conn: sqlite3.Connection, ticker: str) -> list[str]:
    """접두사 유무와 무관하게 같은 종목을 제거한다 (`000660` 으로 `KR:000660` 도 지움)."""
    u = str(ticker).strip().upper()
    _, want = _split_market(u)
    market = u.split(":", 1)[0] if ":" in u else None
    keep = []
    for item in get_watchlist(conn):
        m, t = _split_market(item)
        if t == want and (market is None or m == market):
            continue
        keep.append(item)
    return set_watchlist(conn, keep)


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
    if account.env == "real":
        # 실전 auto 확인 기록. executor 는 이 기록이 없으면 auto 를 off 로 취급한다.
        confirmed = {"confirm": confirm, "at": now_iso()} if mode == "auto" else None
        set_flag(conn, f"live_confirmed:{account.id}", confirmed)
