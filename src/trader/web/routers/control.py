"""control: 제어 API (토큰 필수). 규칙은 Phase 4 함수(control·executor)가 강제한다."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, Request

from trader.control import control
from trader.web.auth import require_token

router = APIRouter(prefix="/control", tags=["control"], dependencies=[Depends(require_token)])


def _set_paused(request: Request, value: bool) -> dict:
    deps = request.app.state.deps
    with closing(deps.conn_factory()) as conn:
        control.set_flag(conn, "paused", value)
        return {"ok": True, "paused": control.is_paused(conn)}


@router.post("/pause")
def pause(request: Request) -> dict:
    return _set_paused(request, True)


@router.post("/resume")
def resume(request: Request) -> dict:
    return _set_paused(request, False)
