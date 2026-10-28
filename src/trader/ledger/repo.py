"""원장 저장소: 체크포인트 적재(멱등), 주문·체결·포지션·판정·승인 기록과 리스크 규칙용 조회.

금액은 Decimal ↔ TEXT 로 저장한다. 날짜 비교는 UTC ISO 문자열(KST 하루 경계는 호출자가 계산).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trader.config import KST, MARKET_TZ
from trader.ledger.db import now_iso

_ANALYST_RE = re.compile(r"^10_analyst_([a-z_]+)$")
_DECISION_RE = re.compile(r"^(30_decision|31_decision_r\d+)$")
# 손절선 바닥(decisions.stop_floor)에 들어가는 결정 action — SELL/TRIM 은 제외(2-O2)
STOP_FLOOR_ACTIONS = ("BUY", "ADD", "HOLD")
# 주문 행의 실행 종류 `orders.origin`(2-O3·2-O8). NULL(옛 행)은 llm. LLM 결정의 주 2회 한도·5일
# 실현손익 성향은 llm·stop 만 센다 — 단타·리밸런스·재호가·헤지가 LLM 결정의 한도를 먹지 않게
ORIGINS = ("llm", "stop", "hedge", "scalp", "rebalance", "reprice", "park", "external")
LLM_ORIGIN_SQL = "COALESCE(origin, 'llm') IN ('llm', 'stop')"

# 살아 있지 않은 주문 상태 (중복 방지·당일 매수·주문금액·보유 폴백·차트 마커가 모두 이 목록을 쓴다).
# expired 는 체결 동기화가 마감 뒤 체결 없는 accepted·unknown 주문에 붙인다 (`fills.sync_fills`).
DEAD_STATUSES = ("rejected", "cancelled", "failed", "expired")


def dead_status_sql() -> str:
    """`status NOT IN (…)` 에 넣을 목록 문자열."""
    return ",".join(f"'{s}'" for s in DEAD_STATUSES)


_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")


def _kst_day_bounds(d: date) -> tuple[str, str]:
    return _day_bounds(d, KST)


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[str, str]:
    """tz 의 날짜 d 하루를 UTC ISO 경계 [start, end) 로."""
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def day_bounds(d: date, market: str | None) -> tuple[str, str]:
    """시장 현지 날짜 d 하루의 UTC ISO 경계. 시장이 없거나 모르면 KST.

    US 는 KST 자정을 넘겨 장이 열리므로 KST 창으로 보면 같은 세션의 주문이 두 날에 갈린다."""
    return _day_bounds(d, MARKET_TZ[market]) if market in MARKET_TZ else _kst_day_bounds(d)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None and str(v) != "" else Decimal(0)


# --- 체크포인트 적재 ---------------------------------------------------------


def ingest_checkpoint(
    conn: sqlite3.Connection, d: date, market: str, ticker: str, stage: str, checkpoint: dict
) -> int | None:
    """10_/20_/30_ 체크포인트를 테이블에 멱등 저장하고 행 id 를 돌려준다. 그 외 단계는 None."""
    data = checkpoint.get("data") if isinstance(checkpoint, dict) else None
    if not isinstance(data, dict):
        return None
    call = checkpoint.get("call") or {}
    created = now_iso()
    js = json.dumps(data, ensure_ascii=False)
    m = _ANALYST_RE.match(stage)
    if m:
        conn.execute(
            "INSERT INTO analyst_reports(date, market, ticker, role, signal, score, confidence, "
            "json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker, role) DO UPDATE SET signal=excluded.signal, "
            "score=excluded.score, confidence=excluded.confidence, json=excluded.json",
            (
                d.isoformat(),
                market,
                ticker,
                m.group(1),
                data.get("signal"),
                data.get("score"),
                data.get("confidence"),
                js,
                created,
            ),
        )
        conn.commit()
        return int(
            conn.execute(
                "SELECT id FROM analyst_reports WHERE date=? AND market=? AND ticker=? AND role=?",
                (d.isoformat(), market, ticker, m.group(1)),
            ).fetchone()["id"]
        )
    m = _DEBATE_RE.match(stage)
    if m:
        conn.execute(
            "INSERT INTO debate_turns(date, market, ticker, side, round, stance, conviction, "
            "json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker, side, round) DO UPDATE SET stance=excluded.stance, "
            "conviction=excluded.conviction, json=excluded.json",
            (
                d.isoformat(),
                market,
                ticker,
                m.group(2),
                int(m.group(1)),
                data.get("stance"),
                data.get("conviction"),
                js,
                created,
            ),
        )
        conn.commit()
        return int(
            conn.execute(
                "SELECT id FROM debate_turns WHERE date=? AND market=? AND ticker=? AND side=? "
                "AND round=?",
                (d.isoformat(), market, ticker, m.group(2), int(m.group(1))),
            ).fetchone()["id"]
        )
    if _DECISION_RE.match(stage):
        # 손절선 바닥(stop_floor): 같은 날 재평가가 json 을 덮어써도 BUY/ADD/HOLD 손절가의 최고값은
        # 남긴다(2-O1). SELL/TRIM 의 손절가는 바닥에 넣지 않는다 — 거부된 매도 재평가가 현재가 바로
        # 아래 값을 적어 보유 묶음 내내 바닥이 되는 것을 막는다(2-O2)
        floor = (
            float(data["stop_loss"])
            if data.get("action") in STOP_FLOOR_ACTIONS and data.get("stop_loss") is not None
            else None
        )
        conn.execute(
            "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, "
            "session_id, json, created_at, stop_floor, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker) DO UPDATE SET action=excluded.action, "
            "target_weight=excluded.target_weight, confidence=excluded.confidence, "
            "session_id=excluded.session_id, json=excluded.json, "
            "stop_floor=CASE WHEN decisions.stop_floor IS NULL THEN excluded.stop_floor "
            "WHEN excluded.stop_floor IS NULL THEN decisions.stop_floor "
            "ELSE MAX(decisions.stop_floor, excluded.stop_floor) END, "
            "updated_at=excluded.updated_at",
            (
                d.isoformat(),
                market,
                ticker,
                data.get("action"),
                data.get("target_weight"),
                data.get("confidence"),
                call.get("session_id"),
                js,
                created,
                floor,
                created,
            ),
        )
        conn.commit()
        return decision_id(conn, d, market, ticker)
    return None


def insert_hedge_decision(
    conn: sqlite3.Connection, d: date, market: str, ticker: str, data: dict
) -> int:
    """파이썬이 만든 헤지 결정 행(source='hedge'). 같은 날·종목이면 갱신. 메모리·분포 제외."""
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, "
        "session_id, json, created_at, source, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'hedge', ?) "
        "ON CONFLICT(date, market, ticker) DO UPDATE SET action=excluded.action, "
        "target_weight=excluded.target_weight, confidence=excluded.confidence, "
        "json=excluded.json, source='hedge', updated_at=excluded.updated_at",
        (
            d.isoformat(),
            market,
            ticker,
            data.get("action"),
            data.get("target_weight"),
            data.get("confidence"),
            json.dumps(data, ensure_ascii=False, default=str),
            now_iso(),
            now_iso(),
        ),
    )
    conn.commit()
    did = decision_id(conn, d, market, ticker)
    assert did is not None
    return did


def set_skipped_debate(conn: sqlite3.Connection, decision_id: int, skipped: bool) -> None:
    """결정 행에 토론 생략 여부(합의 L17)를 남긴다(2-L23). 재평가도 같은 행."""
    conn.execute(
        "UPDATE decisions SET skipped_debate=? WHERE id=?", (int(bool(skipped)), decision_id)
    )
    conn.commit()


def _hydrate_decision(row: sqlite3.Row) -> dict:
    """decisions 행의 json 열을 data 로 복원 (get_decision(hydrate=True)·latest_decision 공통)."""
    d = dict(row)
    d["data"] = json.loads(d.pop("json"))
    return d


def get_decision(
    conn: sqlite3.Connection, decision_id: int, *, hydrate: bool = False
) -> dict | None:
    """결정 행. hydrate=True 면 json 열을 data 로 복원한다 (웹·모니터가 같은 모양을 쓴다)."""
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        return None
    return _hydrate_decision(row) if hydrate else dict(row)


def decision_id(conn: sqlite3.Connection, d: date, market: str, ticker: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM decisions WHERE date=? AND market=? AND ticker=?",
        (d.isoformat(), market, ticker),
    ).fetchone()
    return int(row["id"]) if row else None


# --- 판정·주문·체결 -----------------------------------------------------------


def insert_risk_check(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    account_id: str,
    approved: bool,
    adjusted_qty: int | None,
    reasons: list[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO risk_checks(decision_id, account_id, approved, adjusted_qty, reasons, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            account_id,
            int(approved),
            adjusted_qty,
            json.dumps(reasons, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_order(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    account_id: str,
    ticker: str,
    market: str,
    side: str,
    qty: int,
    order_type: str,
    price: Decimal | None,
    status: str,
    order_no: str | None,
    message: str,
    mode: str,
    created_at: str | None = None,
    amount: Decimal | None = None,
    org_no: str | None = None,
    origin: str | None = None,
) -> int:
    """amount 는 가능금액 차감용 주문금액(시장가는 판정 때 현재가 × 수량). 없으면 qty × price.
    origin 은 실행 종류(`ORIGINS`, 2-O3·2-O8) — None 은 옛 행이라 llm 으로 읽힌다."""
    if origin is not None and origin not in ORIGINS:
        raise ValueError(f"알 수 없는 주문 origin: {origin}")
    if amount is None and price is not None:
        amount = Decimal(qty) * price
    cur = conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, price, "
        "status, order_no, message, mode, created_at, amount, org_no, origin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            account_id,
            ticker,
            market,
            side,
            qty,
            order_type,
            str(price) if price is not None else None,
            status,
            order_no,
            message,
            mode,
            created_at or now_iso(),
            str(amount) if amount is not None else None,
            org_no,
            origin,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_fill(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    account_id: str,
    qty: int,
    price: Decimal,
    filled_at: str | None = None,
) -> int | None:
    """체결 1건 적재. 같은 (계좌, 주문, 시각, 수량, 가격) 은 무시하고 None (재동기화 멱등)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO fills(order_id, account_id, qty, price, filled_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, account_id, qty, str(price), filled_at or now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount == 1 else None


# --- 트리거 (Phase 6 monitor) ------------------------------------------------------


def insert_trigger(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    kind: str,
    detail: dict,
    price: Decimal | None,
    decision_id: int | None,
    account_id: str | None = None,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO triggers(account_id, market, ticker, kind, detail, price, decision_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            market,
            ticker,
            kind,
            json.dumps(detail, ensure_ascii=False, default=str),
            str(price) if price is not None else None,
            decision_id,
            created_at or now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_trigger_handled(
    conn: sqlite3.Connection,
    trigger_id: int,
    session_id: str | None,
    *,
    handled_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE triggers SET handled_at=?, session_id=? WHERE id=?",
        (handled_at or now_iso(), session_id, trigger_id),
    )
    conn.commit()


def reevaluated_between(
    conn: sqlite3.Connection, market: str, ticker: str, kind: str, lo: str, hi: str
) -> bool:
    """[lo, hi) 안에 같은 종류의 트리거가 재평가 세션까지 마쳤는가 (운영일 1회 제한용).

    기동 실패(`session_id` 가 '실패:' 로 시작)는 세지 않아 30분 뒤 재시도할 수 있다."""
    row = conn.execute(
        "SELECT 1 FROM triggers WHERE market=? AND ticker=? AND kind=? "
        "AND handled_at >= ? AND handled_at < ? "
        "AND (session_id IS NULL OR session_id NOT LIKE '실패:%') LIMIT 1",
        (market, ticker, kind, lo, hi),
    ).fetchone()
    return row is not None


def last_trigger_handled_at(conn: sqlite3.Connection, market: str, ticker: str) -> datetime | None:
    """해당 종목의 마지막 재평가 시각 (30분 1회 제한용)."""
    row = conn.execute(
        "SELECT MAX(handled_at) AS t FROM triggers WHERE market=? AND ticker=? "
        "AND handled_at IS NOT NULL",
        (market, ticker),
    ).fetchone()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def has_open_order_or_approval(
    conn: sqlite3.Connection,
    decision_id: int,
    account_id: str,
    side: str | None = None,
    *,
    include_filled: bool = True,
    now: str | None = None,
) -> bool:
    """같은 결정·계좌(·방향)에 살아 있는 주문(실패·거부·취소 제외)이나 pending 승인이 있으면 True.

    재평가(31_decision_r<n>)는 같은 decisions 행을 갱신하므로 방향이 다르면(BUY 뒤 SELL) 허용한다.
    include_filled=False(재평가 라운드)면 이미 **체결 완료**된 주문은 보지 않는다 — 아침 TRIM 이
    체결된 뒤 오후 재평가의 전량 SELL 이 "이미 주문 있음"으로 막히던 문제(전체 검토 O3). 첫 실행의
    재시도(같은 결정 재제출)는 체결분까지 막아 이중 매수를 피한다."""
    dead = dead_status_sql() + ("" if include_filled else ",'filled'")
    sql = f"SELECT 1 FROM orders WHERE decision_id=? AND account_id=? AND status NOT IN ({dead})"
    args: list = [decision_id, account_id]
    if side:
        sql += " AND side=?"
        args.append(side)
    if conn.execute(sql + " LIMIT 1", args).fetchone():
        return True
    sql = "SELECT 1 FROM approvals WHERE decision_id=? AND account_id=? AND status='pending'"
    args = [decision_id, account_id]
    if now is not None:  # 만료됐는데 아직 expired 로 안 바뀐 요청은 살아 있지 않다(검토 O17)
        sql += " AND expires_at > ?"
        args.append(now)
    if side:
        sql += " AND json_extract(order_json, '$.side')=?"
        args.append(side)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def open_orders_today(
    conn: sqlite3.Connection,
    account_id: str,
    ticker: str | None,
    side: str,
    d: date,
    *,
    market: str | None = None,
) -> list[sqlite3.Row]:
    """오늘(시장 현지 하루) 같은 계좌·종목(None 이면 전 종목)·방향의 살아 있고 체결이 끝나지
    않은 주문 행(손절 전·결정 반전·킬스위치 취소 대상)."""
    lo, hi = day_bounds(d, market)
    where, args = "", [account_id, side, lo, hi]
    if ticker is not None:
        where, args = " AND ticker=?", [account_id, side, lo, hi, ticker]
    if market is not None:
        where += " AND market=?"
        args.append(market)
    return conn.execute(
        "SELECT id, ticker, order_no, org_no, qty, market, status, order_type, price FROM orders "
        "WHERE account_id=? AND side=? "
        f"AND status NOT IN ({dead_status_sql()},'filled') "
        "AND created_at >= ? AND created_at < ?" + where + " ORDER BY id",
        args,
    ).fetchall()


def set_order_status(conn: sqlite3.Connection, order_id: int, status: str) -> None:
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()


def has_pending_approval(
    conn: sqlite3.Connection,
    account_id: str,
    ticker: str,
    side: str,
    *,
    market: str,
    now: str,
) -> bool:
    """만료 전 pending 승인 요청이 같은 계좌·시장·종목·방향에 있으면 True (결정 무관 —
    리밸런스처럼 decision_id 가 없는 주문이 같은 감축·증액을 두 번 요청하지 않게)."""
    row = conn.execute(
        "SELECT 1 FROM approvals WHERE account_id=? AND status='pending' AND expires_at > ? "
        "AND json_extract(order_json, '$.ticker')=? AND json_extract(order_json, '$.side')=? "
        "AND json_extract(order_json, '$.market')=? LIMIT 1",
        (account_id, now, ticker, side, market),
    ).fetchone()
    return row is not None


def has_open_order_today(
    conn: sqlite3.Connection,
    account_id: str,
    ticker: str,
    side: str,
    d: date,
    *,
    market: str | None = None,
    exclude_filled: bool = False,
) -> bool:
    """오늘 같은 계좌·종목·방향의 살아 있는 주문이 있으면 True (손절 반복 발주 방지).

    market 을 주면 d 를 그 시장의 현지 날짜로 보고 현지 하루 창을 쓴다 (US 모니터는 KST 자정을
    넘겨 돌므로 KST 창으로는 23:30 KST 주문을 00:30 KST 에 놓친다).
    exclude_filled=True 면 이미 체결이 끝난 주문은 세지 않는다 (오전에 체결된 TRIM 이 잔량의
    손절을 막으면 안 된다; 체결된 주문은 더 이상 "살아 있는" 주문이 아니다).
    """
    lo, hi = day_bounds(d, market)
    excluded = dead_status_sql() + (",'filled'" if exclude_filled else "")
    row = conn.execute(
        "SELECT 1 FROM orders WHERE account_id=? AND ticker=? AND side=? "
        f"AND status NOT IN ({excluded}) "
        "AND created_at >= ? AND created_at < ? LIMIT 1",
        (account_id, ticker, side, lo, hi),
    ).fetchone()
    return row is not None


def latest_trigger_price(
    conn: sqlite3.Connection, market: str, ticker: str, d: date
) -> Decimal | None:
    """운영일 d 의 가장 최근 트리거 가격 (재평가의 '현재가'). 트리거가 없거나 **가장 최근 트리거에
    가격이 없으면**(뉴스 재평가, Phase 23) None — 그때는 다시 수집한 입력의 시세를 쓴다."""
    lo, hi = day_bounds(d, market)
    row = conn.execute(
        "SELECT price FROM triggers WHERE market=? AND ticker=? "
        "AND created_at >= ? AND created_at < ? ORDER BY id DESC LIMIT 1",
        (market, ticker, lo, hi),
    ).fetchone()
    return _dec(row["price"]) if row and row["price"] is not None else None


def get_trigger(conn: sqlite3.Connection, trigger_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, market, ticker, kind, detail, price, decision_id, handled_at FROM triggers "
        "WHERE id=?",
        (trigger_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["detail"] = json.loads(out["detail"]) if out.get("detail") else {}
    except ValueError:
        out["detail"] = {}
    return out


def unhandled_trigger_id(
    conn: sqlite3.Connection, market: str, ticker: str, kind: str, d: date | None = None
) -> int | None:
    """같은 종목·종류의 미처리 트리거 id (있으면 다시 쌓지 않고 그 행을 재평가에 연결한다).
    d 가 있으면 그 운영일(시장 현지 하루)에 쌓인 것만 — 어제 처리 못 한 트리거가 날짜 없이
    이월돼 오늘 재평가가 옛 가격·사유로 돌지 않게 한다."""
    sql = "SELECT id FROM triggers WHERE market=? AND ticker=? AND kind=? AND handled_at IS NULL "
    args: list = [market, ticker, kind]
    if d is not None:
        lo, hi = day_bounds(d, market)
        sql += "AND created_at >= ? AND created_at < ? "
        args += [lo, hi]
    row = conn.execute(sql + "ORDER BY id LIMIT 1", args).fetchone()
    return int(row["id"]) if row else None


def latest_decision(conn: sqlite3.Connection, market: str, ticker: str) -> dict | None:
    """종목의 가장 최근 결정 행 (json → data). monitor 가 손절선·익절·무효화 조건을 읽는다."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE market=? AND ticker=? ORDER BY date DESC, id DESC LIMIT 1",
        (market, ticker),
    ).fetchone()
    return _hydrate_decision(row) if row is not None else None


def max_stop_since(
    conn: sqlite3.Connection, market: str, ticker: str, since: date | None
) -> Decimal | None:
    """보유 묶음 시작일(since) 이후 그 종목 결정들의 손절선 바닥(`stop_floor`) 중 최고값(헤지 결정
    제외). 손절선은 올리기만 한다 — 보유 재분석의 HOLD 가 손절가를 비우거나 낮춰도 이전 손절선을
    지킨다(검토 L5). 같은 날 재평가가 행을 덮어써도 바닥은 `ingest_checkpoint` 가 max 로 남기고
    (2-O1), SELL/TRIM 의 손절가는 바닥에 들어가지 않는다(2-O2). since 가 없으면 None."""
    if since is None:
        return None
    row = conn.execute(
        "SELECT MAX(stop_floor) AS s FROM decisions "
        "WHERE market=? AND ticker=? AND source != 'hedge' AND date >= ? "
        "AND stop_floor IS NOT NULL",
        (market, ticker, since.isoformat()),
    ).fetchone()
    return Decimal(str(row["s"])) if row is not None and row["s"] is not None else None


def today_buy_tickers(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> set[str]:
    """운영일 d(시장 현지 하루)에 낸 살아 있는 BUY 종목. 통화가 달라 market 으로도 거른다."""
    lo, hi = day_bounds(d, market)
    sql = (
        "SELECT DISTINCT ticker FROM orders WHERE account_id=? AND side IN ('BUY') "
        f"AND status NOT IN ({dead_status_sql()}) "
        "AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    return {r["ticker"] for r in conn.execute(sql, args).fetchall()}


def claim_exec_key(conn: sqlite3.Connection, key: str, *, now: str, stale_before: str) -> bool:
    """실행 키 선점(검토 O8). 처음 넣은 프로세스만 True — 살아 있는 키가 있으면(다른 프로세스가 같은
    주문을 실행 중이거나 냈다) False. 중복 확인(has_open_order_or_approval)과 주문 사이 경합을 닫음.

    이미 있는 키도 아래면 조건부 UPDATE 한 번으로 넘겨받는다(영구 차단 방지):
    - 묶인 주문이 죽었다(취소·거부·실패·만료 — 손절 지정가 취소 뒤 재손절, 마감 소멸 뒤 재제출)
    - 브로커로 보내지 않았고(sent_at 없음) 승인 요청도 아닌데 stale_before 보다 오래됐다(선점 뒤
      주문 전에 프로세스가 죽음)
    - 승인 요청 키인데 그 요청이 거절·만료·실패로 닫혔다
    보냈는데 주문 행이 없는 키(접수 뒤 기록 실패)는 넘겨받지 않는다 — 수동 대조 대상."""
    try:
        conn.execute("INSERT INTO exec_keys(key, created_at) VALUES (?, ?)", (key, now))
    except sqlite3.IntegrityError:
        conn.rollback()
    else:
        conn.commit()
        return True
    cur = conn.execute(
        "UPDATE exec_keys SET created_at=?, order_id=NULL, approval_id=NULL, sent_at=NULL "
        "WHERE key=? AND ("
        f"(order_id IS NOT NULL AND order_id IN (SELECT id FROM orders WHERE status IN "
        f"({dead_status_sql()}))) "
        "OR (order_id IS NULL AND sent_at IS NULL AND ("
        "(approval_id IS NULL AND created_at < ?) "
        "OR approval_id IN (SELECT id FROM approvals WHERE status IN "
        "('rejected','expired','failed') OR (status='pending' AND expires_at <= ?)))))",
        (now, key, stale_before, now),
    )
    conn.commit()
    return cur.rowcount == 1


def bind_exec_key(
    conn: sqlite3.Connection,
    key: str,
    *,
    order_id: int | None = None,
    approval_id: int | None = None,
    at: str | None = None,
) -> None:
    """키를 주문 행(접수·unknown) 또는 승인 요청에 묶는다. `bound_at` 은 묶인 시각 — 그 뒤에 잔고를
    본 판정은 이 주문·요청을 스냅샷으로 보지만 그 전에 본 판정에는 진행 중 예약이다(결정 7번)."""
    when = at or now_iso()
    if order_id is not None:
        conn.execute(
            "UPDATE exec_keys SET order_id=?, bound_at=? WHERE key=?", (order_id, when, key)
        )
    if approval_id is not None:
        conn.execute(
            "UPDATE exec_keys SET approval_id=?, bound_at=? WHERE key=?", (approval_id, when, key)
        )
    conn.commit()


def ledger_marks(conn: sqlite3.Connection) -> dict:
    """포트폴리오가 원장을 읽는 순간의 표시 {order_id, approval_id, at}(그때의 최대 id·시각) —
    진행 중 예약(결정 7번)이 '포트폴리오가 이미 센 주문·승인' 을 시각 추정이 아니라 이것으로 가린다
    (PR #67 Codex: 잔고 조회 도중 묶인 주문을 두 번 셌다)."""
    o = conn.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]
    a = conn.execute("SELECT COALESCE(MAX(id), 0) FROM approvals").fetchone()[0]
    return {"order_id": int(o), "approval_id": int(a), "at": now_iso()}


def inflight_reservations(
    conn: sqlite3.Connection,
    account_id: str,
    market: str,
    *,
    marks: dict,
    now: str,
    stale_before: str,
    exclude_key: str | None = None,
) -> list[dict]:
    """판정 포트폴리오에 들어가지 않은 살아 있는 매수 예약 [{key, ticker, amount}](결정 7번).

    포트폴리오는 원장을 읽을 때(`marks` — `ledger_marks`) 있던 체결 전 주문과 pending 승인을 센다.
    그래서 이미 센 것 = 묶인 주문 id ≤ 표시, 또는 묶인 승인 id ≤ 표시이고 그때 pending 이었다(아직
    pending 이거나 표시 시각 뒤에 결정됐다). 그 밖의 살아 있는 예약은 진행 중이다(미묶음·읽기 전에
    승인됐는데 주문 행이 없음·읽은 뒤 생긴 주문). 죽은 주문·닫힌 승인·만료 승인·보내지도 묶이지도
    않은 채 오래된 키는 빼되, **브로커로 보낸 키(sent_at)는 승인이 닫혀도 남긴다** — 접수 뒤 주문 행
    기록이 실패한 주문은 KIS 에 살아 있을 수 있다(PR #67 Codex, 대조·수동 해제 전까지)."""
    rows = conn.execute(
        "SELECT k.key, k.ticker, k.amount FROM exec_keys k "
        "LEFT JOIN orders o ON o.id = k.order_id "
        "LEFT JOIN approvals a ON a.id = k.approval_id "
        "WHERE k.account_id=? AND k.market=? AND k.side='BUY' AND k.amount IS NOT NULL "
        "AND k.key != ? "
        f"AND (o.id IS NULL OR o.status NOT IN ({dead_status_sql()})) "
        "AND (a.id IS NULL OR k.sent_at IS NOT NULL OR (a.status NOT IN "
        "('rejected','expired','failed') AND NOT (a.status='pending' AND a.expires_at <= ?))) "
        "AND NOT (k.order_id IS NULL AND k.approval_id IS NULL AND k.sent_at IS NULL "
        "AND k.created_at < ?) "
        "AND NOT ("
        "(k.order_id IS NOT NULL AND k.order_id <= ?) "
        "OR (k.approval_id IS NOT NULL AND k.approval_id <= ? "
        "AND (a.decided_at IS NULL OR a.decided_at > ?)))",
        (
            account_id,
            market,
            exclude_key or "",
            now,
            stale_before,
            int(marks.get("order_id") or 0),
            int(marks.get("approval_id") or 0),
            str(marks.get("at") or ""),
        ),
    ).fetchall()
    return [{"key": r["key"], "ticker": r["ticker"], "amount": r["amount"]} for r in rows]


def claim_exec_key_reserved(
    conn: sqlite3.Connection,
    key: str,
    *,
    now: str,
    stale_before: str,
    account_id: str,
    market: str,
    ticker: str,
    side: str,
    marks: dict,
    recheck: Callable[[list[dict]], str | None],
) -> tuple[bool, list[dict]]:
    """매수 예약으로 실행 키를 잡는다(결정 7번, 2-O10). 한 쓰기 트랜잭션(BEGIN IMMEDIATE) 안에서
    진행 중 예약을 읽어 `recheck` 에 넘기고, 예약 금액(문자열)을 받으면 키·예약을 함께 쓴다 —
    None 이면 잡지 않는다. 같은 계좌의 다른 경로(분석·재평가·단타·리밸런스·헤지·승인)가 서로의
    예약을 본 뒤에만 키를 잡으므로, 둘 다 같은 여유를 쓰는 경합이 닫힌다. 넘겨받기 규칙은
    `claim_exec_key` 와 같다.
    (잡았나, 본 진행 중 예약)."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        inflight = inflight_reservations(
            conn,
            account_id,
            market,
            marks=marks,
            now=now,
            stale_before=stale_before,
            exclude_key=key,
        )
        amount = recheck(inflight)
        if amount is None:
            conn.rollback()
            return False, inflight
        try:
            conn.execute(
                "INSERT INTO exec_keys(key, created_at, account_id, market, ticker, side, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, now, account_id, market, ticker, side, amount),
            )
        except sqlite3.IntegrityError:
            cur = conn.execute(
                "UPDATE exec_keys SET created_at=?, order_id=NULL, approval_id=NULL, sent_at=NULL, "
                "bound_at=NULL, account_id=?, market=?, ticker=?, side=?, amount=? "
                "WHERE key=? AND ("
                f"(order_id IS NOT NULL AND order_id IN (SELECT id FROM orders WHERE status IN "
                f"({dead_status_sql()}))) "
                "OR (order_id IS NULL AND sent_at IS NULL AND ("
                "(approval_id IS NULL AND created_at < ?) "
                "OR approval_id IN (SELECT id FROM approvals WHERE status IN "
                "('rejected','expired','failed') OR (status='pending' AND expires_at <= ?)))))",
                (now, account_id, market, ticker, side, amount, key, stale_before, now),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False, inflight
        conn.commit()
        return True, inflight
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def mark_exec_key_sent(conn: sqlite3.Connection, key: str, *, sent_at: str) -> None:
    """브로커 호출 직전에 남긴다 — 이후로는 응답이 없어도 주문이 KIS 에 있을 수 있다."""
    conn.execute("UPDATE exec_keys SET sent_at=? WHERE key=?", (sent_at, key))
    conn.commit()


def exec_key_has_approval(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT approval_id FROM exec_keys WHERE key=?", (key,)).fetchone()
    return row is not None and row["approval_id"] is not None


def release_exec_key(conn: sqlite3.Connection, key: str) -> None:
    """브로커가 거부·실패한 주문(KIS 에 주문 없음)·주문 전 예외의 키를 지운다(재시도 허용)."""
    conn.execute("DELETE FROM exec_keys WHERE key=?", (key,))
    conn.commit()


def list_exec_keys(conn: sqlite3.Connection) -> list[dict]:
    """남아 있는 실행 키 전부(오래된 순). `sent_at` 이 있고 `order_id` 가 없는 키는 보낸 뒤
    기록 전에
    끊긴 것 — 사람이 KIS 주문 유무를 확인하고 `trader control exec-key release` 로 푼다(2-O13)."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT key, created_at, order_id, approval_id, sent_at FROM exec_keys "
            "ORDER BY created_at, key"
        ).fetchall()
    ]


def _buy_amounts_today(
    conn: sqlite3.Connection,
    account_id: str,
    d: date,
    market: str | None,
    *,
    exclude_filled: bool,
    now: str | None,
) -> list[tuple[str, Decimal]]:
    """운영일 d 의 살아 있는 BUY 주문 + 만료 전 pending BUY 승인의 (종목, 금액) 목록."""
    lo, hi = day_bounds(d, market)
    excluded = dead_status_sql() + (",'filled'" if exclude_filled else "")
    sql = (
        "SELECT ticker, qty, price, amount FROM orders WHERE account_id=? AND side='BUY' "
        f"AND status NOT IN ({excluded}) AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    out: list[tuple[str, Decimal]] = []
    for r in conn.execute(sql, args).fetchall():
        if r["amount"] is not None:
            out.append((r["ticker"], _dec(r["amount"])))
        else:
            out.append((r["ticker"], Decimal(r["qty"]) * _dec(r["price"])))
    # pending 승인 요청도 현금을 예약한다 — 승인이 누적되면 가능금액을 넘겨 과매수하기 때문
    sql = (
        "SELECT order_json FROM approvals WHERE account_id=? AND status='pending' "
        "AND json_extract(order_json, '$.side')='BUY' AND requested_at >= ? AND requested_at < ?"
    )
    args = [account_id, lo, hi]
    if now is not None:  # 만료 요청은 현금을 더 예약하지 않는다 — expire 잡이 안 돌아도(검토 O17)
        sql += " AND expires_at > ?"
        args.append(now)
    if market:
        sql += " AND json_extract(order_json, '$.market')=?"
        args.append(market)
    for r in conn.execute(sql, args).fetchall():
        o = json.loads(r["order_json"])
        price = o.get("price") if o.get("price") is not None else o.get("quote_price")
        if price is not None:
            out.append((str(o.get("ticker")), Decimal(int(o.get("qty") or 0)) * _dec(price)))
    return out


def pending_buy_amounts(
    conn: sqlite3.Connection,
    account_id: str,
    d: date,
    market: str | None = None,
    *,
    now: str | None = None,
) -> dict[str, Decimal]:
    """체결 전 BUY 주문(accepted·partial·unknown) + pending 승인의 종목별 금액(검토 O10 예상
    포트폴리오). 체결 완료 주문은 브로커 잔고에 이미 보유로 잡히므로 뺀다(partial 은 주문 전체
    금액 — 체결분이 보유와 겹쳐 보수적으로 센다)."""
    out: dict[str, Decimal] = {}
    for t, v in _buy_amounts_today(conn, account_id, d, market, exclude_filled=True, now=now):
        out[t] = out.get(t, Decimal(0)) + v
    return out


def today_order_amount(
    conn: sqlite3.Connection,
    account_id: str,
    d: date,
    market: str | None = None,
    *,
    exclude_filled: bool = False,
    now: str | None = None,
) -> Decimal:
    """운영일 d(시장 현지 하루)의 누적 매수 주문금액. 통화가 달라 market 으로 거른다.
    exclude_filled=True 면 체결 완료 주문은 빼고 미체결(accepted·partial·unknown)만 더한다 —
    주문가능금액(D+2 정산 반영)이 체결분을 이미 뺐을 때 이중 차감을 막는다."""
    return sum(
        (
            v
            for _, v in _buy_amounts_today(
                conn, account_id, d, market, exclude_filled=exclude_filled, now=now
            )
        ),
        Decimal(0),
    )


def trades_this_week(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> dict[str, int]:
    """운영일 d 가 속한 주(월요일 ~ d, 시장 현지 하루 경계)의 종목별 살아 있는 주문 수.
    LLM 결정 주문(llm·stop)만 — 단타·리밸런스·재호가·헤지는 세지 않는다(2-O8)."""
    monday = d - timedelta(days=d.weekday())
    lo, _ = day_bounds(monday, market)
    _, hi = day_bounds(d, market)
    sql = (
        "SELECT ticker, COUNT(*) AS n FROM orders WHERE account_id=? "
        f"AND status NOT IN ({dead_status_sql()}) AND {LLM_ORIGIN_SQL} "
        "AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    rows = conn.execute(sql + " GROUP BY ticker", args).fetchall()
    return {r["ticker"]: int(r["n"]) for r in rows}


def realized_pnl_5d(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal:
    """최근 5거래일(달력 7일 근사) 실현손익: 매도 체결금액 − **현재 보유 묶음의 평균 매수가**.

    창 안에 SELL 체결이 있는 종목만 전체 체결을 시간순으로 걸어 평균가를 갱신한다(보유 0 이면 리셋).
    전 이력 BUY 평균을 쓰면 전량 매도 뒤 재매수한 종목의 손익 부호가 뒤집혀 성향 판정이 틀린다.
    금액은 Decimal 로만 계산한다. 통화가 섞이지 않게 market 으로 거른다. 손익에 넣는 매도는 LLM
    결정 주문(llm·stop)의 체결만 — 단타 왕복 손실이 다음 날 LLM 한도를 줄이지 않게(2-O8) — 이되
    보유 묶음의 수량·평단은 **모든 origin 의 체결**로 재구성한다(재호가 BUY 가 평단이고 리밸런스
    매도가 묶음을 비운다; PR #58 Codex 리뷰).
    """
    lo, _ = day_bounds(d - timedelta(days=7), market)  # 시장 현지 창(US 는 KST 자정을 넘긴다)
    _, hi = day_bounds(d, market)
    origin_sql = LLM_ORIGIN_SQL.replace("origin", "o.origin", 1)
    sql = (
        "SELECT DISTINCT o.ticker FROM fills f JOIN orders o ON o.id=f.order_id "
        f"WHERE f.account_id=? AND o.side='SELL' AND {origin_sql} "
        "AND f.filled_at >= ? AND f.filled_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND o.market=?"
        args.append(market)
    tickers = [r["ticker"] for r in conn.execute(sql, args).fetchall()]
    if not tickers:
        return Decimal(0)
    marks = ",".join("?" * len(tickers))
    sql = (
        "SELECT o.ticker, o.side, f.qty, f.price, f.filled_at, "
        f"({origin_sql}) AS llm FROM fills f JOIN orders o ON o.id=f.order_id "
        f"WHERE f.account_id=? AND o.ticker IN ({marks})"
    )
    args = [account_id, *tickers]
    if market:
        sql += " AND o.market=?"
        args.append(market)
    rows = conn.execute(sql + " ORDER BY f.filled_at, f.id", args).fetchall()
    total = Decimal(0)
    qty: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    for r in rows:
        t = r["ticker"]
        q = int(r["qty"])
        px = _dec(r["price"])
        if r["side"] == "BUY":
            qty[t] = qty.get(t, 0) + q
            cost[t] = cost.get(t, Decimal(0)) + px * q
            continue
        held = qty.get(t, 0)
        if held <= 0:
            # 매수 체결 기록이 없는 보유분(fills sync 이전·HTS 매수): 평단을 몰라 손익에 넣지 않는다
            # (평단 0 으로 치면 매도금액 전체가 이익이 돼 성향이 틀린다)
            continue
        avg = cost.get(t, Decimal(0)) / held
        if r["llm"] and lo <= r["filled_at"] < hi:
            total += (px - avg) * q
        remaining = held - q
        if remaining <= 0:
            qty[t], cost[t] = 0, Decimal(0)
        else:
            qty[t], cost[t] = remaining, avg * remaining
    return total


def _first_buy_of_current_lot(
    events: list[tuple[str, str, int]], market: str | None = None
) -> date | None:
    """(시각, 방향, 수량) 시간순 이벤트에서 현재 보유 묶음의 첫 매수일. 보유가 0 이면 None.

    전량 매도 뒤 다시 사면 그 매수일부터 센다 (최소 보유기간이 재진입에도 적용되도록).
    날짜는 시장 현지(US 는 ET): KST 로 보면 ET 오전 체결이 다음 날이 돼 보유 거래일이 하루 준다."""
    tz = MARKET_TZ.get(market or "", KST)
    qty = 0
    since: date | None = None
    for at, side, n in events:
        if side == "BUY":
            if qty <= 0:
                since = datetime.fromisoformat(at).astimezone(tz).date()
                qty = 0
            qty += int(n)
        else:
            qty -= int(n)
            if qty <= 0:
                qty, since = 0, None
    return since if qty > 0 else None


def held_since(
    conn: sqlite3.Connection, account_id: str, tickers: list[str] | None = None
) -> dict[str, date]:
    """종목별 현재 보유 묶음의 첫 매수일 (최소 보유기간용). 전량 매도 뒤 재매수면 그 날부터.

    체결 기록이 있는 종목은 체결로, 없는 종목은 접수된 주문(매수 +, 매도 −)으로 계산한다.
    보유가 0 인 종목은 돌려주지 않는다. tickers 를 주면 그 종목만 읽는다 (판정마다 계좌 전체
    이력을 읽지 않도록 build_portfolio 가 브로커 보유 종목을 넘긴다; 결과는 같다).
    """
    if tickers is not None and not tickers:
        return {}
    scope, scope_args = "", []
    if tickers:
        scope = f" AND o.ticker IN ({','.join('?' * len(tickers))})"
        scope_args = list(tickers)
    by_ticker: dict[str, list[tuple[str, str, int]]] = {}
    markets: dict[str, str] = {}
    for r in conn.execute(
        "SELECT o.ticker, o.market, o.side, f.qty, f.filled_at FROM fills f "
        f"JOIN orders o ON o.id=f.order_id WHERE f.account_id=?{scope} ORDER BY f.filled_at, f.id",
        (account_id, *scope_args),
    ).fetchall():
        by_ticker.setdefault(r["ticker"], []).append((r["filled_at"], r["side"], r["qty"]))
        markets[r["ticker"]] = r["market"]
    out: dict[str, date] = {}
    for t, events in by_ticker.items():
        since = _first_buy_of_current_lot(events, markets.get(t))
        if since is not None:
            out[t] = since
    fallback: dict[str, list[tuple[str, str, int]]] = {}
    for r in conn.execute(
        "SELECT ticker, market, side, qty, created_at FROM orders o WHERE account_id=? "
        f"AND status NOT IN ({dead_status_sql()}){scope} ORDER BY created_at, id",
        (account_id, *scope_args),
    ).fetchall():
        if r["ticker"] in by_ticker:
            continue  # 체결 기록이 있는 종목은 체결이 정본
        fallback.setdefault(r["ticker"], []).append((r["created_at"], r["side"], r["qty"]))
        markets[r["ticker"]] = r["market"]
    for t, events in fallback.items():
        since = _first_buy_of_current_lot(events, markets.get(t))
        if since is not None:
            out[t] = since
    return out


def snapshot_positions(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    d: date,
    nav: Decimal,
    cash: Decimal,
    positions: list[dict],
    market: str | None = None,
) -> None:
    """계좌·날짜·종목 단위 upsert. nav·cash 는 시장(통화)별이라 _CASH 자리에도 market 을 둔다.

    같은 날 앞선 스냅샷에 있던 종목이 이번에 없으면(전량 매도) 그 행을 지운다 — 웹·트레이더 입력이
    유령 보유를 보지 않도록. 같은 시장의 행만 지운다 (통화·시장 분리)."""
    created = now_iso()
    rows = positions or [
        {
            "ticker": f"_CASH_{market}" if market else "_CASH",
            "market": market,
            "qty": 0,
            "avg_price": None,
            "current_price": None,
            "eval_amount": None,
        }
    ]
    for p in rows:
        conn.execute(
            "INSERT INTO positions_snapshot(account_id, date, ticker, market, qty, avg_price, "
            "current_price, eval_amount, nav, cash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, date, ticker) DO UPDATE SET qty=excluded.qty, "
            "avg_price=excluded.avg_price, current_price=excluded.current_price, "
            # created_at = 마지막 관측 시각(같은 날 다시 읽으면 갱신 — 웹 실시간 지문·신선도)
            "eval_amount=excluded.eval_amount, nav=excluded.nav, cash=excluded.cash, "
            "created_at=excluded.created_at",
            (
                account_id,
                d.isoformat(),
                p["ticker"],
                p.get("market") or market,
                int(p.get("qty", 0)),
                _s(p.get("avg_price")),
                _s(p.get("current_price")),
                _s(p.get("eval_amount")),
                str(nav),
                str(cash),
                created,
            ),
        )
    keep = [p["ticker"] for p in rows]
    marks = ",".join("?" * len(keep))
    conn.execute(
        "DELETE FROM positions_snapshot WHERE account_id=? AND date=? AND market IS ? "
        f"AND ticker NOT IN ({marks})",
        (account_id, d.isoformat(), market, *keep),
    )
    conn.commit()


def nav_history(
    conn: sqlite3.Connection,
    account_id: str,
    market: str,
    *,
    days: int = 30,
    today: date | None = None,
) -> list[tuple[str, Decimal]]:
    """(today 또는 마지막 스냅샷 날짜) − days 이후의 (날짜, NAV) 오름차순 (날짜별 최신 스냅샷).

    스냅샷 개수가 아니라 날짜 창이다: 운영이 띄엄띄엄이어도 오래된 값이 섞이지 않는다.
    **거래일 표본만**: 장 밖 잔고 갱신이 남긴 주말·US 휴장일 날짜는 뺀다 — 데몬을 켜 둔 것만으로
    거래일 수익률·연율화 지표가 바뀌지 않게(GitHub Codex #55). KR 휴장일(평일)은 남는다.
    """
    from trader import market_hours

    if today is None:  # 스냅샷은 판정마다 종목 수만큼 쌓이므로 전 행을 읽지 않고 창만 SQL 로 자른다
        row = conn.execute(
            "SELECT MAX(date) AS d FROM positions_snapshot WHERE account_id=? AND market=? "
            "AND nav IS NOT NULL",
            (account_id, market),
        ).fetchone()
        if not row or not row["d"]:
            return []
        end = date.fromisoformat(row["d"])
    else:
        end = today
    lo = (end - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT date, nav FROM positions_snapshot WHERE account_id=? AND market=? "
        "AND nav IS NOT NULL AND date >= ? GROUP BY date HAVING id = MAX(id) ORDER BY date",
        (account_id, market, lo),
    ).fetchall()
    return [
        (r["date"], _dec(r["nav"]))
        for r in rows
        if market_hours.has_session(market, date.fromisoformat(r["date"]))
    ]


def latest_nav_before(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal | None:
    """d 이전 마지막 스냅샷의 NAV (일일 손익 기준). 시장별 통화가 달라 market 으로 거른다."""
    sql = "SELECT nav FROM positions_snapshot WHERE account_id=? AND date < ?"
    args: list = [account_id, d.isoformat()]
    if market:
        sql += " AND market=?"
        args.append(market)
    row = conn.execute(sql + " ORDER BY date DESC, id DESC LIMIT 1", args).fetchone()
    return _dec(row["nav"]) if row and row["nav"] is not None else None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


# --- 승인 ---------------------------------------------------------------------


def create_approval(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    account_id: str,
    order: dict,
    requested_at: str,
    expires_at: str,
    fingerprint: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO approvals(decision_id, account_id, order_json, status, requested_at, "
        "expires_at, fingerprint) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (
            decision_id,
            account_id,
            json.dumps(order, ensure_ascii=False),
            requested_at,
            expires_at,
            fingerprint,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _approval_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["order"] = json.loads(d.pop("order_json"))
    return d


def get_approval(conn: sqlite3.Connection, approval_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return _approval_row(r) if r else None


def pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id").fetchall()
    return [_approval_row(r) for r in rows]


def expire_pending_buys(conn: sqlite3.Connection, account_id: str, ticker: str) -> int:
    """그 계좌·종목의 pending BUY 승인 요청을 만료로 닫는다(손절 직후 사람이 옛 요청을 승인해 곧바로
    되사는 것을 막는다, 전체 검토 2026-10-27). 닫은 건수."""
    cur = conn.execute(
        "UPDATE approvals SET status='expired', decided_at=? WHERE account_id=? "
        "AND status='pending' AND json_extract(order_json, '$.ticker')=? "
        "AND json_extract(order_json, '$.side')='BUY'",
        (now_iso(), account_id, ticker),
    )
    conn.commit()
    return cur.rowcount


def decide_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    status: str,
    *,
    decided_at: str | None = None,
    expect: str = "pending",
) -> bool:
    """`expect` 상태일 때만 바꾼다(조건부 UPDATE). 다른 프로세스가 먼저 처리했으면 False."""
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status=?",
        (status, decided_at or now_iso(), approval_id, expect),
    )
    conn.commit()
    return cur.rowcount == 1


def close_stale_running(conn: sqlite3.Connection, *, older_than_iso: str) -> int:
    """older_than 이전에 시작해 아직 running 인 agent_calls 를 error 로 닫는다(재기동 정리)."""
    cur = conn.execute(
        "UPDATE agent_calls SET status='error', error='stale: 1시간 넘게 running' "
        "WHERE status='running' AND created_at < ?",
        (older_than_iso,),
    )
    conn.commit()
    return cur.rowcount


# --- 종목명 (Phase 16) ---------------------------------------------------------


def upsert_ticker_name(
    conn: sqlite3.Connection, market: str, ticker: str, name: str | None, source: str
) -> bool:
    """관측한 종목명을 적는다(최근 관측 우선). 빈 이름·코드와 같은 이름은 무시한다."""
    clean = (name or "").strip()
    if not clean or clean == ticker:
        return False
    conn.execute(
        "INSERT INTO ticker_names(market, ticker, name, source, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET name=excluded.name, source=excluded.source, "
        "updated_at=excluded.updated_at WHERE name IS NOT excluded.name "
        "OR source IS NOT excluded.source",  # 같은 값이면 쓰기(WAL·잠금) 없음
        (market, ticker, clean, source, now_iso()),
    )
    conn.commit()
    return True


def upsert_ticker_exchange(
    conn: sqlite3.Connection, market: str, ticker: str, exchange: str | None
) -> bool:
    """관측한 US 거래소 코드(잔고 `ovrs_excg_cd`·00_inputs `meta.exchange`)를 남긴다(2-L1) —
    유니버스 밖 보유·미만기 창 종목의 일일 피처 스냅샷이 거래소를 알 수 있게. KR·빈 값은 무시,
    같은 값이면 쓰기 없음. 썼으면 True."""
    clean = (exchange or "").strip()
    if market != "US" or not clean:
        return False
    cur = conn.execute(
        "INSERT INTO ticker_exchanges(market, ticker, exchange, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET exchange=excluded.exchange, "
        "updated_at=excluded.updated_at WHERE exchange IS NOT excluded.exchange",
        (market, ticker, clean, now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


def ticker_exchanges(conn: sqlite3.Connection, market: str) -> dict[str, str]:
    """시장의 관측 거래소 {ticker: exchange}."""
    return {
        r["ticker"]: r["exchange"]
        for r in conn.execute(
            "SELECT ticker, exchange FROM ticker_exchanges WHERE market=?", (market,)
        )
    }


def held_tickers_latest(conn: sqlite3.Connection, market: str) -> set[str]:
    """계좌마다 가장 최근 잔고 스냅샷에서 수량 > 0 인 종목(현금 자리 제외)의 합집합 — 열린 창
    종목(2-L1) 의 보유 부분."""
    rows = conn.execute(
        "SELECT DISTINCT p.ticker FROM positions_snapshot p JOIN ("
        "SELECT account_id, MAX(date) AS d FROM positions_snapshot WHERE market=? "
        "GROUP BY account_id) l ON l.account_id = p.account_id AND l.d = p.date "
        "WHERE p.market=? AND p.qty > 0 AND p.ticker NOT LIKE '\\_CASH%' ESCAPE '\\'",
        (market, market),
    ).fetchall()
    return {str(r["ticker"]) for r in rows}


def ticker_names(conn: sqlite3.Connection, market: str | None = None) -> dict[tuple[str, str], str]:
    sql = "SELECT market, ticker, name FROM ticker_names"
    params: tuple = ()
    if market is not None:
        sql += " WHERE market=?"
        params = (market,)
    return {(r["market"], r["ticker"]): r["name"] for r in conn.execute(sql, params)}
