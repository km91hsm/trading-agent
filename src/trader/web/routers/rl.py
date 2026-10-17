"""rl: RL 사이저 실험 로그 (레지스트리 요약·메타·섀도 기록·비교 리포트). 조회 전용."""

from __future__ import annotations

import re
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query, Request

from trader.ml import registry
from trader.ml.rl import report, shadow
from trader.web.routers._common import deps_of, require_account, require_market

router = APIRouter(prefix="/rl", tags=["rl"])
_VERSION_RE = re.compile(r"^\d{8}_\d{6}$")


@router.get("/experiments")
def experiments(request: Request) -> dict:
    metas = registry.list_models(report.NAME, deps_of(request).state)
    active = next((m for m in metas if m.get("promoted")), None)
    return {
        "experiments": [report.model_summary(m) for m in metas],
        "active": report.model_summary(active) if active else None,
    }


@router.get("/experiments/{version}")
def experiment_detail(request: Request, version: str) -> dict:
    if not _VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail="버전 형식 오류")
    for m in registry.list_models(report.NAME, deps_of(request).state):
        if m.get("version") == version:
            return m
    raise HTTPException(status_code=404, detail=f"실험 없음: {version}")


@router.get("/shadow")
def shadow_rows(
    request: Request, account_id: str | None = None, days: int = Query(30, ge=1, le=365)
) -> dict:
    acct = require_account(request, account_id)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        return {"rows": shadow.recent(conn, acct.id, days=days)}


@router.get("/report")
def comparison(
    request: Request,
    account_id: str | None = None,
    market: str | None = None,
    weeks: int = Query(8, ge=1, le=52),
) -> dict:
    acct = require_account(request, account_id)
    if market is not None:
        market = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        return report.comparison(conn, deps.state, account_id=acct.id, market=market, weeks=weeks)
