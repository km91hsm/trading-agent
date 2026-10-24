"""jobs: 작업 대기열 (Phase 17). 조회는 토큰 없음, 요청·취소는 토큰.

실행 주체는 데몬의 `job_queue` 잡뿐이라 데몬이 살아 있지 않으면 요청을 받지 않는다(409).
kind·params 검증은 `trader.jobs.validate` 하나다.
"""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from trader import heartbeat
from trader import jobs as jobq
from trader.platform import paths
from trader.web.auth import require_token
from trader.web.routers._common import deps_of

DAEMON_DOWN = "데몬이 꺼져 있어요 — 요청은 데몬이 실행해요 (Trader.app 또는 trader daemon)"

public_router = APIRouter(prefix="/jobs", tags=["jobs"])
router = APIRouter(prefix="/control/jobs", tags=["jobs"], dependencies=[Depends(require_token)])


class JobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    params: dict = {}


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int


def daemon_alive(request: Request) -> bool:
    deps = deps_of(request)
    base = deps.state if deps.state is not None else paths.state_dir()
    return bool(heartbeat.read_status(base, deps.now(), [])["alive"])


def _with_labels(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["label"] = jobq.label(r["kind"], r["params"])
    return rows


@public_router.get("")
def list_jobs(request: Request, limit: int = Query(30, ge=1, le=200)) -> dict:
    """대기·실행 중(오래된 순) + 최근 종료분. `alive` 는 데몬 생존."""
    with closing(deps_of(request).conn_factory()) as conn:
        rows = jobq.list_recent(conn, limit=limit)
        active = jobq.active_count(conn)
    return {"alive": daemon_alive(request), "active": active, "requests": _with_labels(rows)}


@router.post("")
def request_job(request: Request, body: JobBody) -> dict:
    try:
        params = jobq.validate(body.kind, body.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not daemon_alive(request):
        raise HTTPException(status_code=409, detail=DAEMON_DOWN)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        try:
            rid = jobq.enqueue(conn, body.kind, params, source="web", now=deps.now())
        except jobq.DuplicateRequest as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "id": rid,
        "kind": body.kind,
        "params": params,
        "status": "queued",
        "label": jobq.label(body.kind, params),
    }


@router.post("/cancel")
def cancel_job(request: Request, body: CancelBody) -> dict:
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        if not jobq.cancel(conn, body.id, now=deps.now()):
            raise HTTPException(status_code=409, detail="대기 중인 요청만 취소할 수 있어요")
    return {"id": body.id, "status": "skipped"}
