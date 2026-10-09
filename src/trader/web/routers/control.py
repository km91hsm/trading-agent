"""control: 제어 API (토큰 필수). 규칙은 Phase 4 함수(control·executor)가 강제한다.

승인·거부는 executor.approve/reject(만료·1회·조건부 UPDATE)를, 모드는 control.set_account_mode
(실전 auto 는 confirm="LIVE")를 그대로 부른다. 웹이 브로커를 직접 호출하지 않는다.
"""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from trader.control import control
from trader.web.auth import require_token
from trader.web.routers._common import deps_of

router = APIRouter(prefix="/control", tags=["control"], dependencies=[Depends(require_token)])


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: int


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    mode: str
    confirm: str | None = None


def _exec_ctx(request: Request, conn):
    from trader.orchestrator.executor import ExecContext

    deps = deps_of(request)
    return ExecContext(
        settings=deps.settings,
        accounts=deps.accounts,
        conn=conn,
        brokers=deps.brokers,
        notifier=deps.notifier,
        state=deps.state,
        now=deps.now,
    )


def _decide(request: Request, approval_id: int, action: str) -> dict:
    from trader.orchestrator import executor

    with closing(deps_of(request).conn_factory()) as conn:
        ectx = _exec_ctx(request, conn)
        fn = executor.approve if action == "approve" else executor.reject
        r = fn(ectx, approval_id)
    if not r["ok"]:
        order = r.get("order")
        if order is not None:  # 승인은 됐으나 브로커가 거부·실패 → 게이트웨이 오류
            raise HTTPException(
                status_code=502, detail=f"브로커 거부: {order.get('message') or '알 수 없음'}"
            )
        raise HTTPException(status_code=409, detail=r.get("error") or f"{action} 실패")
    return r


@router.post("/approve")
def approve(request: Request, body: ApprovalBody) -> dict:
    return _decide(request, body.approval_id, "approve")


@router.post("/reject")
def reject(request: Request, body: ApprovalBody) -> dict:
    return _decide(request, body.approval_id, "reject")


@router.post("/mode")
def mode(request: Request, body: ModeBody) -> dict:
    deps = deps_of(request)
    acct = deps.account(body.account_id)
    if acct is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 계좌: {body.account_id}")
    with closing(deps.conn_factory()) as conn:
        try:
            control.set_account_mode(conn, acct, body.mode, confirm=body.confirm)
        except control.ConfirmationRequired as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"account_id": acct.id, "mode": control.get_account_mode(conn, acct)}


def _set_paused(request: Request, value: bool) -> dict:
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        control.set_flag(conn, "paused", value)
        return {"ok": True, "paused": control.is_paused(conn)}


@router.post("/pause")
def pause(request: Request) -> dict:
    return _set_paused(request, True)


@router.post("/resume")
def resume(request: Request) -> dict:
    return _set_paused(request, False)
