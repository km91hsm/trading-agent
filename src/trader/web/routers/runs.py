"""runs: 실행 기록·호출 비용·오늘의 단계·체크포인트·결정 (경로 탈출은 _validate_ids 가 막는다)."""

from __future__ import annotations

from contextlib import closing
from datetime import date as _date

from fastapi import APIRouter, HTTPException, Query, Request

from trader.ledger import views
from trader.orchestrator import run_store, tools
from trader.orchestrator.run_store import list_checkpoints, read_checkpoint
from trader.platform import paths
from trader.web.routers._common import deps_of, require_market, today_kst

router = APIRouter(tags=["runs"])


def _date_arg(request: Request, value: str | None) -> _date:
    if not value:
        return today_kst(request)
    try:
        return _date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date 형식 오류: {value!r}") from None


def _ids(ticker: str | None, market: str | None, stage: str | None = None) -> None:
    try:
        tools._validate_ids(ticker or "", market or "", stage)
    except tools.ToolInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.get("/runs")
def runs(request: Request, limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    with closing(deps_of(request).conn_factory()) as conn:
        return views.list_runs(conn, limit=limit)


@router.get("/calls")
def calls(request: Request, days: int = Query(7, ge=1, le=365)) -> dict:
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        rows = views.list_agent_calls(conn, days=days, now=deps.now())
        start = tools.sessions_today(conn, deps.now())
        cost = sum(float(r["cost_usd"] or 0) for r in rows if _is_today(deps, r["created_at"]))
    return {"calls": rows, "sessions_today": start, "cost_today": round(cost, 4)}


def _is_today(deps, created_at: str) -> bool:
    from datetime import datetime

    from trader.web.routers._common import KST

    try:
        return (
            datetime.fromisoformat(created_at).astimezone(KST).date()
            == deps.now().astimezone(KST).date()
        )
    except ValueError:
        return False


@router.get("/today")
def today(request: Request, market: str | None = None, date: str | None = None) -> dict:
    m = require_market(market)
    d = _date_arg(request, date)
    deps = deps_of(request)
    mdir = run_store.market_dir(d, m, deps.state)
    tickers: dict[str, list[str]] = {}
    if mdir.exists():
        for p in sorted(mdir.iterdir()):
            if p.is_dir() and (stages := list_checkpoints(p)):  # 빈 디렉터리는 숨긴다
                tickers[p.name] = stages
    return {"date": d.isoformat(), "market": m, "tickers": tickers}


@router.get("/checkpoint")
def checkpoint(
    request: Request,
    ticker: str | None = None,
    market: str | None = None,
    date: str | None = None,
    stage: str | None = None,
) -> dict:
    _ids(ticker, market, stage or "")
    d = _date_arg(request, date)
    deps = deps_of(request)
    base = deps.state if deps.state is not None else paths.state_dir()
    # run_dir 은 디렉터리를 만들므로 조회에서는 경로만 조립한다 (존재하지 않으면 404)
    d_path = base / "runs" / d.isoformat() / market / ticker  # type: ignore[operator]
    cp = read_checkpoint(d_path, stage) if d_path.is_dir() else None  # type: ignore[arg-type]
    if cp is None:
        raise HTTPException(status_code=404, detail=f"체크포인트 없음: {stage}")
    return {
        "stage": stage,
        "schema_version": cp.get("schema_version"),
        "data": cp.get("data", cp),
        "call": cp.get("call"),
    }


@router.get("/decisions")
def decisions(
    request: Request, days: int = Query(30, ge=1, le=3650), market: str | None = None
) -> list[dict]:
    deps = deps_of(request)
    if market is not None:
        require_market(market)
    with closing(deps.conn_factory()) as conn:
        return views.list_decisions(conn, days=days, market=market, now=deps.now())


@router.get("/decisions/{decision_id}/outcomes")
def decision_outcomes(request: Request, decision_id: int) -> dict:
    with closing(deps_of(request).conn_factory()) as conn:
        dec = views.get_decision(conn, decision_id)
        if dec is None:
            raise HTTPException(status_code=404, detail=f"결정 없음: {decision_id}")
        return {"decision": dec, "outcomes": views.decision_outcomes(conn, decision_id)}
