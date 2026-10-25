"""웹 UI 용 읽기 전용 조회 (스펙 §8). 계좌 단위 조회는 전부 account_id 로 거른다.

반환은 JSON 직렬화 가능한 dict 목록이며 금액은 TEXT(Decimal 문자열) 그대로 둔다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.config import KST
from trader.ledger import metrics, repo

MARKETS = ("KR", "US")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def list_orders(conn: sqlite3.Connection, account_id: str, *, limit: int = 100) -> list[dict]:
    return _rows(
        conn.execute(
            "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit)
        )
    )


def list_approvals(
    conn: sqlite3.Connection, account_id: str, *, status: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM approvals WHERE account_id=?"
    args: list = [account_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    out = []
    for r in conn.execute(sql + " ORDER BY id DESC", args).fetchall():
        d = dict(r)
        d["order"] = json.loads(d.pop("order_json"))
        out.append(d)
    return out


def list_risk_checks(conn: sqlite3.Connection, account_id: str, *, limit: int = 100) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT rc.*, d.ticker, d.market, d.date, d.action FROM risk_checks rc "
        "LEFT JOIN decisions d ON d.id=rc.decision_id WHERE rc.account_id=? "
        "ORDER BY rc.id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall():
        d = dict(r)
        d["approved"] = bool(d["approved"])
        d["reasons"] = json.loads(d["reasons"] or "[]")
        out.append(d)
    return out


def latest_positions(conn: sqlite3.Connection, account_id: str, market: str) -> dict | None:
    """해당 계좌·시장의 최신 스냅샷 날짜 포지션 (현금 자리 행 제외)."""
    row = conn.execute(
        "SELECT date, nav, cash FROM positions_snapshot WHERE account_id=? AND market=? "
        "ORDER BY date DESC, id DESC LIMIT 1",
        (account_id, market),
    ).fetchone()
    if row is None:
        return None
    positions = _rows(
        conn.execute(
            "SELECT ticker, market, qty, avg_price, current_price, eval_amount "
            "FROM positions_snapshot WHERE account_id=? AND market=? AND date=? "
            "AND ticker NOT LIKE '\\_CASH%' ESCAPE '\\' ORDER BY ticker",
            (account_id, market, row["date"]),
        )
    )
    return {"date": row["date"], "nav": row["nav"], "cash": row["cash"], "positions": positions}


def sector_weights(
    positions: list[dict],
    nav: str | Decimal | None,
    sector_of: Callable[[str], str | None] | None,
    *,
    hedge_tickers: set[str] | frozenset[str] = frozenset(),
    cash: str | Decimal | None = None,
) -> list[dict]:
    """섹터별 평가금액·비중 (비중 내림차순). 모르는 종목은 '기타', 헤지 ETF 는 '헤지', 현금은
    '현금' 행으로 합이 NAV 가 되게 한다. nav 가 없거나 0 이면 비중은 None."""
    nav_d = Decimal(str(nav)) if nav not in (None, "") else None
    buckets: dict[str, dict] = {}
    for p in positions:
        t = str(p.get("ticker"))
        if t in hedge_tickers:
            sector = "헤지"
        else:
            sector = (sector_of(t) if sector_of else None) or "기타"
        b = buckets.setdefault(sector, {"sector": sector, "eval_amount": Decimal(0), "tickers": []})
        b["eval_amount"] += Decimal(str(p.get("eval_amount") or 0))
        b["tickers"].append(t)
    if cash not in (None, ""):
        buckets["현금"] = {"sector": "현금", "eval_amount": Decimal(str(cash)), "tickers": []}
    rows = sorted(buckets.values(), key=lambda b: (-b["eval_amount"], b["sector"]))
    out = []
    for b in rows:
        w = float(b["eval_amount"] / nav_d) if nav_d and nav_d > 0 else None
        out.append(
            {
                "sector": b["sector"],
                "eval_amount": str(b["eval_amount"]),
                "weight": w,
                "tickers": b["tickers"],
                "count": len(b["tickers"]),
            }
        )
    return out


def list_decisions(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    market: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    since = ((now or datetime.now(UTC)).astimezone(KST).date() - timedelta(days=days)).isoformat()
    sql = (
        "SELECT id, date, market, ticker, action, target_weight, confidence, session_id, "
        "created_at, source FROM decisions WHERE date >= ?"
    )
    args: list = [since]
    if market:
        sql += " AND market=?"
        args.append(market)
    return _rows(conn.execute(sql + " ORDER BY date DESC, id DESC", args))


def get_decision(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    return repo.get_decision(conn, decision_id, hydrate=True)


def decision_outcomes(conn: sqlite3.Connection, decision_id: int) -> list[dict]:
    """계좌별 판정 + (있으면) 마지막 주문 상태."""
    out = []
    for r in conn.execute(
        "SELECT account_id, approved, adjusted_qty, reasons, created_at FROM risk_checks "
        "WHERE decision_id=? ORDER BY id",
        (decision_id,),
    ).fetchall():
        o = conn.execute(
            "SELECT status, order_no, qty, price, mode FROM orders WHERE decision_id=? "
            "AND account_id=? ORDER BY id DESC LIMIT 1",
            (decision_id, r["account_id"]),
        ).fetchone()
        out.append(
            {
                "account_id": r["account_id"],
                "approved": bool(r["approved"]),
                "adjusted_qty": r["adjusted_qty"],
                "reasons": json.loads(r["reasons"] or "[]"),
                "checked_at": r["created_at"],
                "order_status": o["status"] if o else None,
                "order_no": o["order_no"] if o else None,
                "order_qty": o["qty"] if o else None,
                "order_price": o["price"] if o else None,
                "order_mode": o["mode"] if o else None,
            }
        )
    return out


_ROLE_LABEL = {
    "technical": "차트",
    "fundamental": "회사가치",
    "news_macro": "뉴스·거시",
    "sentiment": "투자자 분위기",
}


def decision_trace(
    conn: sqlite3.Connection, decision_id: int, state: Path | None = None
) -> dict | None:
    """결정 하나의 판단 과정(Phase 23 E): 그 종목 run 디렉터리의 체크포인트를 한 번에 요약한다 —
    애널리스트 4(역할·신호·확신·근거 2개) → 토론(라운드별 stance·conviction) → 수렴 요약 →
    결정 → 게이트(계좌별 승인·사유·note) → 주문(계좌별 상태) + 재평가·손절·재호가 라운드.
    체크포인트 읽기는 run_store 함수로만 한다."""
    from trader.orchestrator.run_store import list_checkpoints, read_checkpoint, run_dir

    dec = repo.get_decision(conn, decision_id, hydrate=True)
    if dec is None:
        return None
    d = date.fromisoformat(str(dec["date"]))
    rd = run_dir(d, dec["market"], dec["ticker"], state)
    names = list_checkpoints(rd)

    def data(stage: str) -> dict:
        cp = read_checkpoint(rd, stage) or {}
        return cp.get("data") if isinstance(cp.get("data"), dict) else {}

    analysts = []
    for n in names:
        if not n.startswith("10_analyst_"):
            continue
        a = data(n)
        role = str(a.get("role") or n[len("10_analyst_") :])
        analysts.append(
            {
                "role": role,
                "label": _ROLE_LABEL.get(role, role),
                "signal": a.get("signal"),
                "confidence": a.get("confidence"),
                "summary": a.get("summary"),
                "evidence": [
                    e.get("claim") for e in (a.get("evidence") or [])[:2] if isinstance(e, dict)
                ],
            }
        )
    debate = []
    for n in names:
        if not n.startswith("20_debate_"):
            continue
        t = data(n)
        debate.append(
            {
                "round": t.get("round"),
                "side": t.get("side"),
                "stance": t.get("stance"),
                "conviction": t.get("conviction"),
                "timing": t.get("timing"),
            }
        )
    debate.sort(key=lambda x: (int(x.get("round") or 0), str(x.get("side"))))
    rs = data("25_research_summary")
    research = (
        {k: rs.get(k) for k in ("rounds", "gap_round0", "gap_final", "sycophantic", "preferred")}
        if rs
        else None
    )
    inputs = data("00_inputs")
    quote = ((inputs.get("technical") or {}).get("quote") or {}).get("price")

    def by_account(prefix: str) -> list[dict]:
        out = []
        for n in names:
            if not n.startswith(prefix + "_"):
                continue
            acct = n[len(prefix) + 1 :]
            v = data(n)
            out.append({"stage": n, "account_id": acct, **_gate_or_order(v)})
        return out

    rounds = sorted(
        {n.split("_")[2] for n in names if n.startswith("31_decision_r")}, key=lambda r: int(r[1:])
    )
    return {
        "decision": dec,
        "quote": quote,
        "analysts": analysts,
        "debate": debate,
        "research": research,
        "gate": by_account("40_risk_check"),
        "orders": by_account("50_order"),
        "reevaluations": [
            {
                "round": r,
                "decision": data(f"31_decision_{r}"),
                "gate": by_account(f"41_risk_check_{r}"),
                "orders": by_account(f"51_order_{r}"),
            }
            for r in rounds
        ],
        "stop_loss": {"gate": by_account("45_stop_loss"), "orders": by_account("55_order_sl")},
        "reprice": {"gate": by_account("42_reprice"), "orders": by_account("52_order_reprice")},
        "stages": names,
    }


def _gate_or_order(v: dict) -> dict:
    if "approved" in v:
        return {
            "approved": bool(v.get("approved")),
            "qty": v.get("adjusted_qty"),
            "reasons": v.get("reasons") or [],
            "note": v.get("note"),
            "mode": v.get("mode"),
            "dry_run": v.get("dry_run"),
        }
    res = v.get("result") or {}
    req = v.get("request") or {}
    return {
        "status": v.get("status"),
        "order_no": res.get("order_no"),
        "message": res.get("message"),
        "side": req.get("side"),
        "qty": req.get("qty"),
        "price": req.get("price"),
        "mode": v.get("mode"),
    }


def list_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))


def list_agent_calls(
    conn: sqlite3.Connection, *, days: int = 7, now: datetime | None = None
) -> list[dict]:
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    return _rows(
        conn.execute("SELECT * FROM agent_calls WHERE created_at >= ? ORDER BY id DESC", (since,))
    )


def account_summary(conn: sqlite3.Connection, account_id: str, today: date) -> dict:
    nav_by_market: dict[str, str | None] = {}
    for m in MARKETS:
        snap = conn.execute(
            "SELECT nav FROM positions_snapshot WHERE account_id=? AND market=? "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (account_id, m),
        ).fetchone()
        nav_by_market[m] = snap["nav"] if snap else None
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE account_id=? AND status='pending'",
        (account_id,),
    ).fetchone()["n"]
    return {
        "stance_by_market": metrics.stance_by_market(conn, account_id, today, MARKETS),
        "nav_by_market": nav_by_market,
        "pending_approvals": int(pending),
    }
