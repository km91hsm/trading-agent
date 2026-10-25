"""runs: 실행 기록·호출 비용·오늘의 단계·체크포인트·결정 (경로 탈출은 _validate_ids 가 막는다)."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date as _date

from fastapi import APIRouter, HTTPException, Query, Request

from trader.ledger import views
from trader.orchestrator import run_store, tools
from trader.orchestrator.run_store import list_checkpoints, read_checkpoint
from trader.platform import paths
from trader.web.routers._common import csv_response, deps_of, require_market, today_kst

router = APIRouter(tags=["runs"])


def _date_arg(request: Request, value: str | None, market: str | None = None) -> _date:
    if not value:
        # US 는 KST 자정 뒤에도 전날 뉴욕 운영일 디렉터리에 실행이 있다 → run_date_for
        if market in ("KR", "US"):
            return run_store.run_date_for(market, deps_of(request).now())
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
    d = _date_arg(request, date, m)
    deps = deps_of(request)
    mdir = run_store.market_dir(d, m, deps.state)
    tickers: dict[str, list[str]] = {}
    if mdir.exists():
        for p in sorted(mdir.iterdir()):
            if p.is_dir() and (stages := list_checkpoints(p)):  # 빈 디렉터리는 숨긴다
                tickers[p.name] = stages
    return {
        "date": d.isoformat(),
        "market": m,
        "tickers": tickers,
        "scan": scan_summary(mdir),
        "market_files": market_files(mdir),
        "news_jobs": news_jobs(deps.conn_factory, m, d),
    }


def news_jobs(conn_factory, market: str, d) -> list[dict]:
    """장중 뉴스 발굴(Phase 23)이 낸 요청(`job_requests.source='news'`) — 그 운영일의 시장분.
    종목 재분석(analyze_ticker·reevaluate)과 발굴 세션(discover)이 함께 나온다."""
    from trader.ledger import repo

    lo, hi = repo.day_bounds(d, market)
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT id, kind, params, status, requested_at, finished_at, error FROM job_requests "
            "WHERE source='news' AND requested_at >= ? AND requested_at < ? "
            "ORDER BY id DESC LIMIT 200",
            (lo, hi),
        ).fetchall()
    except sqlite3.OperationalError:  # 스키마가 아직 없는 빈 원장(테스트 픽스처·첫 실행)
        rows = []
    finally:
        conn.close()
    out = []
    for r in rows:
        params = json.loads(r["params"]) if r["params"] else {}
        if params.get("market") != market:
            continue
        out.append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "ticker": params.get("ticker"),
                "status": r["status"],
                "requested_at": r["requested_at"],
                "finished_at": r["finished_at"],
                "error": r["error"],
            }
        )
    return out


_MARKET_STAGE_RE = re.compile(r"^_[a-z0-9_]{1,40}$")


def market_files(mdir) -> list[str]:
    """시장 단위 단계 파일(`_market_*`·`_scan`·`_screen`·`_discovery*`) 이름 목록(정렬)."""
    if not mdir.exists():
        return []
    return sorted(
        p.stem for p in mdir.glob("_*.json") if p.is_file() and _MARKET_STAGE_RE.match(p.stem)
    )


@router.get("/market-checkpoint")
def market_checkpoint(
    request: Request, market: str | None = None, date: str | None = None, stage: str | None = None
) -> dict:
    """시장 단위 단계 파일 하나(`_market_inputs`·`_market_view`·`_scan` …). 오늘의 실행 페이지가
    시장 세션 결과를 보여 줄 때 쓴다(Phase 21)."""
    m = require_market(market)
    if not stage or not _MARKET_STAGE_RE.match(stage):
        raise HTTPException(status_code=400, detail=f"시장 단계 이름 형식 오류: {stage!r}")
    d = _date_arg(request, date, m)
    mdir = run_store.market_dir(d, m, deps_of(request).state)
    cp = read_checkpoint(mdir, stage) if mdir.is_dir() else None
    if cp is None:
        raise HTTPException(status_code=404, detail=f"시장 단계 파일 없음: {stage}")
    if isinstance(cp, dict) and "data" in cp:
        return {"stage": stage, "schema_version": cp.get("schema_version"), "data": cp["data"]}
    return {"stage": stage, "schema_version": None, "data": cp}


def scan_summary(mdir) -> dict | None:
    """`_scan.json` 의 picks(사유·뉴스 발굴 촉매)와 발굴 요약. 없으면 None."""
    scan = read_checkpoint(mdir, "_scan") if mdir.exists() else None
    if not scan:
        return None
    disc = scan.get("discovery") or {}
    by_ticker = {c.get("ticker"): c for c in disc.get("candidates") or []}
    picks = []
    for p in scan.get("picks") or []:
        c = by_ticker.get(p.get("ticker"))
        picks.append(
            {
                "ticker": p.get("ticker"),
                "reason": p.get("reason"),
                "discovery": (
                    {"tier": c.get("tier"), "catalyst": c.get("catalyst")} if c else None
                ),
            }
        )
    from trader.scan import discovery as discovery_mod

    return {
        "picks": picks,
        "targets": len(scan.get("analyze_targets") or []),
        "cap_hit": bool(scan.get("cap_hit")),
        "discovery": disc or None,
        "discovery_line": discovery_mod.line(disc or None),
    }


@router.get("/checkpoint")
def checkpoint(
    request: Request,
    ticker: str | None = None,
    market: str | None = None,
    date: str | None = None,
    stage: str | None = None,
) -> dict:
    _ids(ticker, market, stage or "")
    d = _date_arg(request, date, market)
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
    request: Request,
    days: int = Query(30, ge=1, le=3650),
    market: str | None = None,
    format: str | None = None,
):
    deps = deps_of(request)
    if market is not None:
        require_market(market)
    with closing(deps.conn_factory()) as conn:
        rows = views.list_decisions(conn, days=days, market=market, now=deps.now())
    if format == "csv":  # Phase 23 G
        return csv_response(rows, f"decisions_{days}d.csv")
    return rows


@router.get("/decisions/{decision_id}/trace")
def decision_trace(request: Request, decision_id: int) -> dict:
    """결정의 판단 과정 한 묶음(Phase 23 E): 애널리스트 → 토론 → 결정 → 게이트 → 주문."""
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        tr = views.decision_trace(conn, decision_id, deps.state)
    if tr is None:
        raise HTTPException(status_code=404, detail=f"결정 없음: {decision_id}")
    return tr


@router.get("/decisions/{decision_id}/outcomes")
def decision_outcomes(request: Request, decision_id: int) -> dict:
    with closing(deps_of(request).conn_factory()) as conn:
        dec = views.get_decision(conn, decision_id)
        if dec is None:
            raise HTTPException(status_code=404, detail=f"결정 없음: {decision_id}")
        return {"decision": dec, "outcomes": views.decision_outcomes(conn, decision_id)}
