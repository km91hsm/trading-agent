"""파생 레인 조회 API(Phase 26 단계 4, 토큰 없음·읽기 전용). 웹은 브로커를 부르지 않고 원장
(`fo_positions`·`fo_order_legs`·`fo_decisions`·`fo_nav_snapshot`)과 체크포인트만 읽는다.
계좌 단위 조회는 `account_id`(파생 계좌) 필수."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from trader.config import AccountConfig
from trader.control import control
from trader.fo import sizing, store
from trader.ledger import performance, repo
from trader.orchestrator import run_store
from trader.web.routers._common import deps_of, today_kst

router = APIRouter(prefix="/fo", tags=["fo"])

MARKET = "KR"


def _fo_account(request: Request, account_id: str) -> AccountConfig:
    acct = next((a for a in deps_of(request).fo_accounts if a.id == account_id), None)
    if acct is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 파생 계좌: {account_id}")
    return acct


def _s(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


def _decision_row(r) -> dict:
    try:
        data = json.loads(r["data"]) if r["data"] else {}
    except ValueError:
        data = {}
    dec = data.get("decision") or {}
    return {
        "id": r["id"], "date": r["date"], "account_id": r["account_id"],
        "position_id": r["position_id"], "round": r["round"], "intent": r["intent"],
        "direction": r["direction"], "vehicle": r["vehicle"],
        "target_leverage": r["target_leverage"], "confidence": r["confidence"],
        "index_at_decision": r["index_at_decision"], "stop_pt": dec.get("stop_pt"),
        "take_profit_pt": dec.get("take_profit_pt"), "horizon_days": dec.get("horizon_days"),
        "rationale": dec.get("rationale"), "vs_spot": dec.get("vs_spot"),
        "record_only": data.get("record_only"), "created_at": r["created_at"],
    }  # fmt: skip


VEHICLES = ("FUTURES", "OPTION_BUY", "OPTION_SELL")
# 게이트 판정(FoVerdict) 중 화면에 펼쳐 보이는 값
VERDICT_FIELDS = (
    "leverage_used", "exposure_after", "stress_loss_after", "lifetime_budget",
    "cross_factor", "n_target", "approved_mult", "code", "kind", "contract_side",
)  # fmt: skip


def _account_status(conn, deps, a: AccountConfig, today: date) -> dict:
    """대시보드 요약·파생 화면 계좌 상태 한 묶음(원장·control 만 — 브로커 호출 없음).
    state 우선순위: 실전 파생 off → 레인 꺼짐(disabled) → 검증 전(unverified) → 일일 손실 한도
    (halted) → 기록만(record_only: 모드 off·일시정지) → active."""
    from trader.fo.executor import CONTRACT_VERSION, fo_mode, verified_key

    cfg = deps.settings.fo
    mode, note = fo_mode(conn, a)
    paused = control.is_paused(conn)
    verified = {
        v: control.get_flag(conn, verified_key(deps.settings, a, v)) == CONTRACT_VERSION
        for v in VEHICLES
    }
    halted = control.get_flag(conn, f"fo_halted:{a.id}") == today.isoformat()
    pending = isinstance(control.get_flag(conn, f"fo_pending_entry:{a.id}"), dict)
    nav_row = conn.execute(
        "SELECT date, nav, cash, margin_total, add_margin FROM fo_nav_snapshot "
        "WHERE account_id = ? ORDER BY date DESC LIMIT 1",
        (a.id,),
    ).fetchone()
    nav = Decimal(str(nav_row["nav"])) if nav_row and nav_row["nav"] not in (None, "") else None
    margin = (
        Decimal(str(nav_row["margin_total"]))
        if nav_row and nav_row["margin_total"] not in (None, "")
        else None
    )
    positions = [
        {"code": p.code, "kind": p.kind, "contract_side": p.contract_side,
         "direction": p.direction, "qty": p.qty_held, "status": p.status}
        for p in store.open_positions(conn, a.id)
        if p.status not in ("settled", "closed")
    ]  # fmt: skip
    if a.env != "paper":
        state = "off"
    elif not cfg.enabled:
        state = "disabled"
    elif not any(verified[v] for v in cfg.vehicles):
        state = "unverified"
    elif halted:
        state = "halted"
    elif mode != "auto" or paused:
        state = "record_only"
    else:
        state = "active"
    rows = conn.execute(
        "SELECT * FROM fo_decisions WHERE date = ? AND (account_id IS NULL OR account_id = ?) "
        "ORDER BY round DESC, id DESC LIMIT 1",
        (today.isoformat(), a.id),
    ).fetchall()
    return {
        "id": a.id, "env": a.env, "mode": mode, "mode_note": note, "paused": paused,
        "state": state, "enabled": cfg.enabled, "vehicles": list(cfg.vehicles),
        "verified": verified, "halted": halted, "pending_entry": pending,
        "nav_date": nav_row["date"] if nav_row else None, "nav": _s(nav),
        "cash": nav_row["cash"] if nav_row else None, "margin_total": _s(margin),
        "add_margin": nav_row["add_margin"] if nav_row else None,
        "margin_pct": round(float(margin / nav), 4) if nav and margin is not None and nav > 0
        else None,
        "positions": positions,
        "decision": _decision_row(rows[0]) if rows else None,
    }  # fmt: skip


@router.get("/summary")
def summary(request: Request) -> dict:
    """대시보드 선물 한 줄 요약 — 파생 계좌마다 상태·잔고·포지션·오늘 결정. 계좌 없으면 빈 목록."""
    deps = deps_of(request)
    today = today_kst(request)
    with closing(deps.conn_factory()) as conn:
        return {"accounts": [_account_status(conn, deps, a, today) for a in deps.fo_accounts]}


@router.get("/account")
def account(request: Request, account_id: str = Query(...)) -> dict:
    """파생 화면 상단: 계좌 상태 + 시장·한도 맥락(오늘 `_fo_inputs` 가 있으면 그것, 없으면 설정·
    control) + 오늘 트리거."""
    from trader.fo.inputs import INPUTS_STAGE, limits_of

    deps = deps_of(request)
    a = _fo_account(request, account_id)
    today = today_kst(request)
    with closing(deps.conn_factory()) as conn:
        status = _account_status(conn, deps, a, today)
        regime = control.get_flag(conn, f"regime:{MARKET}") or {}
        view = control.get_flag(conn, f"market_view:{MARKET}") or {}
        lo, hi = repo.day_bounds(today, MARKET)
        trig = [
            {"kind": str(r["kind"]).removeprefix("fo:"), "code": r["ticker"],
             "price": r["price"], "at": r["created_at"], "handled": r["handled_at"] is not None,
             "session": r["session_id"]}
            for r in conn.execute(
                "SELECT * FROM triggers WHERE kind LIKE 'fo:%' AND account_id = ? "
                "AND created_at >= ? AND created_at < ? ORDER BY id DESC",
                (a.id, lo, hi),
            )
        ]  # fmt: skip
    mdir = run_store.market_dir(today, MARKET, deps.state)
    inputs = run_store.read_checkpoint(mdir, INPUTS_STAGE)
    ctx = (((inputs or {}).get("data") or {}).get("context") or {}).get("data") or {}
    if ctx:
        context = {
            "source": "inputs", "index": ctx.get("index"), "regime": ctx.get("regime"),
            "market_view": ctx.get("market_view"), "limits": ctx.get("limits"),
            "products": ctx.get("products"),
        }  # fmt: skip
    else:
        rstate = regime.get("state") if isinstance(regime, dict) else None
        context = {
            "source": "settings", "index": None,
            "regime": {"state": rstate, "count": regime.get("count") if isinstance(regime, dict)
                       else None},
            "market_view": {k: view.get(k) for k in ("stance", "confidence", "date")}
            if isinstance(view, dict) and view else None,
            "limits": limits_of(deps.settings.fo, rstate),
            "products": None,
        }  # fmt: skip
    return {"status": status, "context": context, "triggers": trig}


@router.get("/today")
def today(request: Request, date_: str | None = Query(None, alias="date")) -> dict:
    """오늘(또는 date) 아침 결정·재평가 라운드·계좌별 판정 요약·보류·일일 손실 상태."""
    deps = deps_of(request)
    d = date.fromisoformat(date_) if date_ else today_kst(request)
    with closing(deps.conn_factory()) as conn:
        rows = conn.execute(
            "SELECT * FROM fo_decisions WHERE date = ? ORDER BY round, id", (d.isoformat(),)
        ).fetchall()
        decisions = [_decision_row(r) for r in rows]
        accounts = []
        for a in deps.fo_accounts:
            accounts.append({
                "id": a.id, "env": a.env,
                "halted": control.get_flag(conn, f"fo_halted:{a.id}") == d.isoformat(),
                "pending_entry": isinstance(
                    control.get_flag(conn, f"fo_pending_entry:{a.id}"), dict
                ),
            })  # fmt: skip
    mdir = run_store.market_dir(d, MARKET, deps.state)
    checks = {}
    for stage in run_store.list_checkpoints(mdir):
        if stage.startswith("_fo_risk_check_") or stage.startswith("_fo_order_"):
            cp = run_store.read_checkpoint(mdir, stage) or {}
            data = cp.get("data") or {}
            verdict = data.get("verdict") or {}
            checks[stage] = {
                "approved": verdict.get("approved", data.get("approved")),
                "reasons": (verdict.get("reasons") or data.get("reasons") or [])[:5],
                "note": verdict.get("note") or data.get("note"),
                "status": data.get("status"), "record_only": data.get("record_only"),
                "error": data.get("error"),
                **{k: verdict.get(k) for k in VERDICT_FIELDS if k in verdict},
            }  # fmt: skip
    morning = next((x for x in decisions if x["round"] == 0), None)
    return {
        "date": d.isoformat(), "decision": morning,
        "reevals": [x for x in decisions if x["round"] > 0], "accounts": accounts,
        "checks": checks,
    }  # fmt: skip


@router.get("/positions")
def positions(request: Request, account_id: str = Query(...)) -> list[dict]:
    """열린 포지션(정산·청산 끝난 것 제외) + 최근 잔고 스냅샷의 평균가·현재가·평가 + 수명 예산."""
    deps = deps_of(request)
    acct = _fo_account(request, account_id)
    r = deps.settings.fo.risk_per_trade_pct
    out = []
    with closing(deps.conn_factory()) as conn:
        for p in store.open_positions(conn, acct.id):
            snap = conn.execute(
                "SELECT avg, current, eval FROM fo_positions_snapshot WHERE account_id = ? AND "
                "code = ? ORDER BY date DESC LIMIT 1",
                (acct.id, p.code),
            ).fetchone()
            avg = Decimal(snap["avg"]) if snap and snap["avg"] else None
            budget = sizing.position_budget(
                p, ref_price=avg, multiplier=deps.settings.fo.product_cfg().multiplier, r=r
            )
            live = store.live_legs(conn, position_id=p.id)
            out.append({
                "id": p.id, "code": p.code, "kind": p.kind, "contract_side": p.contract_side,
                "direction": p.direction, "vehicle": p.vehicle, "status": p.status,
                "qty_held": p.qty_held, "qty_target": p.qty_target,
                "leverage_used": p.leverage_used, "stop_pt": _s(p.stop_pt),
                "stop_fut": _s(p.stop_fut), "tp_pt": _s(p.tp_pt),
                "premium_stop": _s(p.premium_stop), "entry_premium": _s(p.entry_premium),
                "approved_mult": p.approved_mult, "last_trade_date": p.last_trade_date,
                "horizon_until": p.horizon_until, "opened_at": p.opened_at,
                "avg": snap["avg"] if snap else None, "current": snap["current"] if snap else None,
                "eval": snap["eval"] if snap else None,
                "budget_used": _s(budget["used"]), "budget_planned": _s(budget["planned"]),
                "budget_limit": _s(budget["limit"]), "budget_free": _s(budget["free"]),
                "live_legs": len(live), "note": p.note,
            })  # fmt: skip
    return out


@router.get("/legs")
def legs(
    request: Request, account_id: str = Query(...), date_: str | None = Query(None, alias="date")
) -> list[dict]:
    """그날(KST) 만든 주문 다리 + 아직 살아 있는 다리."""
    deps = deps_of(request)
    acct = _fo_account(request, account_id)
    d = date.fromisoformat(date_) if date_ else today_kst(request)
    lo, hi = repo.day_bounds(d, MARKET)
    with closing(deps.conn_factory()) as conn:
        rows = conn.execute(
            "SELECT l.*, p.code FROM fo_order_legs l JOIN fo_positions p ON p.id = l.position_id "
            "WHERE l.account_id = ? AND ((l.created_at >= ? AND l.created_at < ?) OR l.status IN "
            "('prepared','sending','accepted','unknown')) ORDER BY l.id DESC",
            (acct.id, lo, hi),
        ).fetchall()
    return [
        {
            "id": x["id"], "position_id": x["position_id"], "code": x["code"],
            "intent": x["intent"], "round": x["round"], "attempt": x["attempt"],
            "status": x["status"], "order_no": x["order_no"], "qty": x["qty"],
            "filled_qty": x["filled_qty"], "price": x["price"], "order_type": x["order_type"],
            "sent_at": x["sent_at"], "created_at": x["created_at"],
        }  # fmt: skip
        for x in rows
    ]


@router.get("/scorecard")
def scorecard(request: Request) -> dict:
    """만기가 채점된 파생 결정(`fo_decisions.matured_at`)의 방향 적중률·계약 손익 합."""
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        rows = conn.execute(
            "SELECT direction_hit, contract_pnl FROM fo_decisions WHERE matured_at IS NOT NULL "
            "AND direction != 'FLAT'"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM fo_decisions").fetchone()[0]
    hits = [r["direction_hit"] for r in rows if r["direction_hit"] is not None]
    pnl = sum((Decimal(str(r["contract_pnl"])) for r in rows if r["contract_pnl"]), Decimal(0))
    return {
        "decisions": int(total), "matured": len(rows),
        "hit_rate": (sum(hits) / len(hits)) if hits else None, "contract_pnl": str(pnl),
    }  # fmt: skip


@router.get("/report")
def report(request: Request, account_id: str = Query(...), days: int = Query(30, ge=1, le=365)):
    """파생 계좌 NAV 추이(`fo_nav_snapshot`) — 현물 성과 페이지와 같은 (날짜, NAV) 모양."""
    deps = deps_of(request)
    acct = _fo_account(request, account_id)
    with closing(deps.conn_factory()) as conn:
        series = performance.nav_series(conn, acct.id, MARKET, days=days, asset="fo")
    first = series[0][1] if series else None
    last = series[-1][1] if series else None
    ret = float(last / first - 1) if first and last and first > 0 else None
    return {
        "account_id": acct.id, "days": days,
        "nav": [{"date": d, "nav": str(v)} for d, v in series], "return": ret,
    }  # fmt: skip
