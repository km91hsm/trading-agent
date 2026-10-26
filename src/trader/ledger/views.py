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


def _dist(target, current) -> float | None:
    """목표가의 현재가 대비 거리(소수). 어느 쪽이든 없거나 0 이면 None."""
    try:
        t, c = Decimal(str(target)), Decimal(str(current))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if t <= 0 or c <= 0:
        return None
    return float(t / c - 1)


def position_plan(decision: dict | None, current_price) -> dict | None:
    """보유 행의 계획(Phase 24): 최근 결정의 목표 비중·익절·손절·지정가·보유기간과 현재가 대비 거리.
    결정이 없거나 헤지 결정이면 None."""
    from trader.risk import horizon

    if not decision or decision.get("source") == "hedge":
        return None
    data = decision.get("data") or {}
    days = data.get("horizon_days")
    return {
        "decision_id": decision.get("id"),
        "date": decision.get("date"),
        "action": decision.get("action"),
        "target_weight": data.get("target_weight", decision.get("target_weight")),
        "take_profit": data.get("take_profit"),
        "stop_loss": data.get("stop_loss"),
        "limit_price": data.get("limit_price"),
        "horizon_days": days,
        "horizon": horizon.name_of(days),
        "dist_take_profit_pct": _dist(data.get("take_profit"), current_price),
        "dist_stop_pct": _dist(data.get("stop_loss"), current_price),
        "dist_limit_pct": _dist(data.get("limit_price"), current_price),
    }


def attach_plans(conn: sqlite3.Connection, market: str, snap: dict | None) -> None:
    """스냅샷 보유 행에 `plan`·`weight`(평가/NAV) 와 하위 호환 `horizon`·`horizon_days` 를
    붙인다."""
    if not snap:
        return
    nav = Decimal(str(snap.get("nav") or 0))
    for p in snap.get("positions") or []:
        plan = position_plan(
            repo.latest_decision(conn, market, p["ticker"]), p.get("current_price")
        )
        p["plan"] = plan
        p["horizon_days"] = plan["horizon_days"] if plan else None
        p["horizon"] = plan["horizon"] if plan else None
        ev = Decimal(str(p.get("eval_amount") or 0))
        p["weight"] = float(ev / nav) if nav > 0 else None
        # 목표 비중 대비 드리프트(%p, Phase 25 리밸런스): 현재 − 목표. HOLD·SELL 은 목표 없음
        tgt = (plan or {}).get("target_weight")
        p["drift_pp"] = (
            round((p["weight"] - float(tgt)) * 100, 2)
            if p["weight"] is not None
            and tgt is not None
            and plan["action"] in ("BUY", "ADD", "TRIM")
            else None
        )


def _env_block(
    conn: sqlite3.Connection,
    snaps: list[tuple],
    market: str,
    sector_of: Callable[[str], str | None] | None,
    hedge: set[str],
) -> dict:
    """한 시장·한 환경(모의|실전) 계좌들의 합산: NAV·현금·평가손익·보유(같은 종목 합침, 수량 가중
    평단)·섹터 비중. 비중·섹터의 분모는 이 환경의 NAV 뿐이다."""
    nav = cash = cost = ev_sum = Decimal(0)
    merged: dict[str, dict] = {}
    accts = []
    for a, snap in snaps:
        nav += Decimal(str(snap["nav"] or 0))
        cash += Decimal(str(snap["cash"] or 0))
        accts.append(
            {
                "id": a.id,
                "env": a.env,
                "nav": snap["nav"],
                "cash": snap["cash"],
                "snapshot_date": snap["date"],
            }
        )
        for p in snap["positions"]:
            qty = int(p["qty"] or 0)
            if qty <= 0:
                continue
            avg = Decimal(str(p["avg_price"] or 0))
            ev = Decimal(str(p["eval_amount"] or 0))
            g = merged.setdefault(
                p["ticker"],
                {
                    "ticker": p["ticker"],
                    "qty": 0,
                    "cost": Decimal(0),
                    "eval": Decimal(0),
                    "current_price": p["current_price"],
                    "accounts": [],
                },
            )
            g["qty"] += qty
            g["cost"] += avg * qty
            g["eval"] += ev
            g["accounts"].append(a.id)
            cost += avg * qty
            ev_sum += ev
    positions = []
    for g in sorted(merged.values(), key=lambda g: -g["eval"]):
        avg = g["cost"] / g["qty"] if g["qty"] else Decimal(0)
        pnl = g["eval"] - g["cost"]
        plan = position_plan(repo.latest_decision(conn, market, g["ticker"]), g["current_price"])
        positions.append(
            {
                "ticker": g["ticker"],
                "market": market,
                "qty": g["qty"],
                "avg_price": str(avg),
                "current_price": g["current_price"],
                "eval_amount": str(g["eval"]),
                "pnl": str(pnl),
                "pnl_pct": float(pnl / g["cost"]) if g["cost"] else None,
                "weight": float(g["eval"] / nav) if nav > 0 else None,
                "accounts": g["accounts"],
                "plan": plan,
                # 계좌 표(`attach_plans`)와 같은 최상위 필드 — 보유기간 열이 읽는다
                "horizon_days": plan["horizon_days"] if plan else None,
                "horizon": plan["horizon"] if plan else None,
            }
        )
    unmapped = [
        p["ticker"]
        for p in positions
        if p["ticker"] not in hedge and not (sector_of and sector_of(p["ticker"]))
    ]
    pnl_all = ev_sum - cost
    return {
        "nav": str(nav),
        "cash": str(cash),
        "pnl": str(pnl_all),
        "pnl_pct": float(pnl_all / cost) if cost else None,
        "positions": positions,
        "sectors": sector_weights(positions, nav, sector_of, hedge_tickers=hedge, cash=cash),
        "unmapped": unmapped,
        "accounts": accts,
    }


def account_overview(
    conn: sqlite3.Connection,
    accounts: list,
    *,
    sector_of: Callable[[str, str], str | None] | None,
    hedge_tickers: Callable[[str], set[str]],
    sector_resolvers: dict[str, Callable[[str], str | None]] | None = None,
) -> dict:
    """전체 계좌 합산(Phase 24). 통화가 달라 시장별로, 가짜 돈과 진짜 돈이라 모의·실전(env)별로만
    더한다(PRODUCT.md 원칙 3) — 보유·비중·섹터의 분모도 환경별 NAV. `markets[시장].envs[env]` 가
    `_env_block`. 스냅샷이 없는 계좌는 `accounts_missing_snapshot[시장]`, 계좌 설정 수는
    `configured`(스냅샷이 하나도 없는 새 설치와 계좌가 없는 설정을 구분)."""
    markets = [m for m in MARKETS if any(m in a.markets for a in accounts)]
    out: dict = {"markets": {}, "accounts_missing_snapshot": {}, "configured": len(accounts)}
    for m in markets:
        by_env: dict[str, list[tuple]] = {}
        missing = []
        for a in accounts:
            if m not in a.markets:
                continue
            snap = latest_positions(conn, a.id, m)
            if snap is None:
                missing.append(a.id)
                continue
            by_env.setdefault(a.env, []).append((a, snap))
        if sector_resolvers and m in sector_resolvers:  # yaml + 자동 분류 캐시('자동' 꼬리표)
            smap = sector_resolvers[m]
        else:
            smap = (lambda t, _m=m: sector_of(t, _m)) if sector_of else None
        hedge = hedge_tickers(m)
        out["markets"][m] = {
            "envs": {
                env: _env_block(conn, by_env[env], m, smap, hedge)
                for env in ("paper", "real")
                if env in by_env
            }
        }
        out["accounts_missing_snapshot"][m] = missing
    out["pending_approvals"] = conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE status='pending'"
    ).fetchone()[0]
    return out


def sector_weights(
    positions: list[dict],
    nav: str | Decimal | None,
    sector_of: Callable[[str], str | None] | None,
    *,
    hedge_tickers: set[str] | frozenset[str] = frozenset(),
    cash: str | Decimal | None = None,
) -> list[dict]:
    """섹터별 평가금액·비중 (비중 내림차순). 모르는 종목은 '기타', 헤지 ETF 는 '헤지', 현금은
    '현금' 행으로 합이 NAV 가 되게 한다. nav 가 없거나 0 이면 비중은 None.
    sector_of 가 `auto_tickers`(자동 분류 캐시로 정해진 종목 집합, `sector_suggest.SectorResolver`)
    를 가지면 그 종목이 든 행에 `auto: true` 를 붙인다."""
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
    auto = getattr(sector_of, "auto_tickers", None) or set()
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
                "auto": any(t in auto for t in b["tickers"]),
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
        {
            k: rs.get(k)
            for k in (
                "rounds",
                "gap_round0",
                "gap_final",
                "sycophantic",
                "preferred",
                "skipped_debate",
                "reason",
            )
        }
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
