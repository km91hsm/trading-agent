"""control: 제어 API (토큰 필수). 규칙은 Phase 4 함수(control·executor)가 강제한다.

승인·거부는 executor.approve/reject(만료·1회·조건부 UPDATE)를, 모드는 control.set_account_mode
(실전 auto 는 confirm="LIVE")를 그대로 부른다. 웹이 브로커를 직접 호출하지 않는다.
"""

from __future__ import annotations

from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from trader import accounts_io
from trader.accounts_io import AccountSpec
from trader.config import ConfigError
from trader.control import control
from trader.web.auth import require_direct_local, require_token
from trader.web.routers._common import deps_of, require_market

router = APIRouter(prefix="/control", tags=["control"], dependencies=[Depends(require_token)])


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: int


class ThrottleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None  # None 이면 화면 설정을 지우고 yaml 값으로


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    mode: str
    confirm: str | None = None


class WatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "remove"]
    ticker: str


class ManualInputBody(BaseModel):
    """'읽은 기사 붙여 넣기': 시장 공통 수동 입력 한 항목 (`_market_<시장>.md`)."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["KR", "US"]
    title: str
    url: str = ""
    note: str = ""


# 조회는 토큰 없음(다른 조회 API 와 같은 규칙), 변경은 위 router(토큰 필수)
public_router = APIRouter(prefix="/control", tags=["control"])


@public_router.get("/watchlist")
def watchlist(request: Request) -> dict:
    with closing(deps_of(request).conn_factory()) as conn:
        return {"tickers": control.get_watchlist(conn)}


@router.post("/watchlist")
def watchlist_change(request: Request, body: WatchBody) -> dict:
    with closing(deps_of(request).conn_factory()) as conn:
        current = control.get_watchlist(conn)
        try:
            if body.action == "add":
                out = control.set_watchlist(conn, [*current, body.ticker])
            else:
                out = control.remove_watch(conn, body.ticker)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"tickers": out}


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
        if order is not None and order.get("status") == "unknown":
            # 요청은 나갔는데 응답이 없다 — 접수됐을 수 있어 '거부'로 안내하지 않는다(검토 W2).
            # 다시 누르면 중복 주문이 될 수 있어 200 + unknown 으로 돌려주고 화면이 경고한다
            return {
                **r,
                "unknown": True,
                "message": "주문 응답을 받지 못했어요. 접수됐을 수 있으니 다시 누르지 말고 "
                "KIS 앱에서 확인하세요. 체결 동기화가 상태를 확정해요.",
            }
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
    acct = deps.any_account(body.account_id)  # 설정 화면은 파생 계좌 모드도 바꾼다(Phase 26)
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


@router.post("/saveticker/restart", dependencies=[Depends(require_direct_local)])
def saveticker_restart(request: Request) -> dict:
    """세이브티커 '다시 시작': 정지·작업 쉬기 기록을 지워 데몬이 1분 안에 서비스를 다시 띄우게 한다
    (인증 실패를 반복 로그인으로 밀어붙이지 않도록 사람이 누를 때만)."""
    import logging

    from trader import saveticker as st
    from trader.saveticker import automation

    home = st.home()
    if automation.status(home).get("backend") == "extension":
        raise HTTPException(409, "일반 Chrome 확장 아이콘에서 재개해 주세요")
    automation.clear_blocks(home)
    logging.getLogger("trader.web").info("세이브티커 다시 시작 요청(웹)")
    return {"configured": st.credentials_path().is_file(), **automation.status(home)}


@router.post("/usage-throttle")
def usage_throttle(request: Request, body: ThrottleBody) -> dict:
    """Claude 구독 사용률 감속 켜기·끄기(설정 화면). 끄면 사용률과 무관하게 세션을 띄운다 —
    하루 세션 상한(`llm.daily_session_cap`)은 그대로."""
    from trader import usage

    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        control.set_flag(conn, usage.THROTTLE_FLAG, body.enabled)
        enabled, source = usage.throttle_enabled(conn, deps.settings)
    return {"enabled": enabled, "source": source}


@router.post("/pause")
def pause(request: Request) -> dict:
    return _set_paused(request, True)


@router.post("/resume")
def resume(request: Request) -> dict:
    return _set_paused(request, False)


@public_router.get("/manual-inputs")
def manual_inputs_list(request: Request, market: str | None = None) -> dict:
    """시장 공통 수동 입력 목록(최신 순). 종목별 파일은 포함하지 않는다."""
    from trader.sources import manual_inputs

    deps = deps_of(request)
    m = require_market(market)
    return {"market": m, "items": manual_inputs.read_market(m, state=deps.state)}


@router.post("/manual-inputs")
def manual_inputs_add(request: Request, body: ManualInputBody) -> dict:
    from trader.sources import manual_inputs

    deps = deps_of(request)
    try:
        item = manual_inputs.append_market(
            body.market,
            title=body.title,
            url=body.url,
            note=body.note,
            now=deps.now(),
            state=deps.state,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return item


# --- 연결 키(secrets/.env) -------------------------------------------------------
# 값은 돌려주지 않고 '설정됨' 여부만. 쓰기·조회 모두 토큰 + 이 PC 직접 접속(tailnet 폰 요청 거부).
# 실행 중인 데몬·웹은 시작할 때 읽은 키를 쓰므로 바꾼 키는 재시작 뒤에 적용된다


class SecretsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str] = {}
    clear: list[str] = []


@router.post("/secrets/status", dependencies=[Depends(require_direct_local)])
def secrets_status(request: Request) -> dict:
    from trader import secrets_io
    from trader.platform import paths

    return secrets_io.status(paths.secrets_path(), deps_of(request).all_accounts)


@router.post("/secrets", dependencies=[Depends(require_direct_local)])
def secrets_update(request: Request, body: SecretsBody) -> dict:
    import logging

    from trader import secrets_io
    from trader.platform import paths

    try:
        r = secrets_io.update(
            paths.secrets_path(),
            deps_of(request).all_accounts,
            values=body.values,
            clear=body.clear,
        )
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logging.getLogger("trader.web").info(
        "연결 키 변경(웹): 설정 %s · 지움 %s", r["updated"], r["cleared"]
    )
    return {**r, "restart_required": bool(r["updated"] or r["cleared"])}


# --- 계좌 추가·삭제(config/accounts.yaml) ------------------------------------------
# 연결 키와 같은 규칙(토큰 + 이 PC 직접 접속). 계좌 목록은 시작할 때 읽으므로 재시작 뒤에 적용된다.
# 계좌번호는 받지 않는다(자리표시자 — 실제 번호는 연결 키의 KIS_<ID>_CANO)


class AccountRemoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str


def _accounts_path():
    from trader.platform import paths

    return paths.config_dir() / "accounts.yaml"


@router.post("/accounts", dependencies=[Depends(require_direct_local)])
def account_add(request: Request, spec: AccountSpec) -> dict:
    import logging

    try:
        r = accounts_io.add_account(_accounts_path(), spec)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logging.getLogger("trader.web").info("계좌 추가(웹): %s (%s·%s)", spec.id, spec.env, spec.asset)
    return {**r, "restart_required": True}


@router.post("/accounts/remove", dependencies=[Depends(require_direct_local)])
def account_remove(request: Request, body: AccountRemoveBody) -> dict:
    import logging

    try:
        with closing(deps_of(request).conn_factory()) as conn:
            r = accounts_io.remove_account(_accounts_path(), body.account_id, conn)
    except accounts_io.AccountInUse as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logging.getLogger("trader.web").info("계좌 삭제(웹): %s", body.account_id)
    return {**r, "restart_required": True}


@public_router.get("/bootstrap-token")
def bootstrap_token(request: Request) -> dict:
    """이 PC 브라우저가 직접 연 같은 출처 화면에만 웹 토큰을 준다(첫 실행에 .env 를 열어 복사하지
    않게). 다른 사이트의 요청(Sec-Fetch-Site ≠ same-origin)·tailnet 폰 요청은 거부."""
    require_direct_local(request)
    if request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(status_code=403, detail="같은 출처 화면에서만 받을 수 있습니다")
    token = deps_of(request).token
    if not token:
        raise HTTPException(status_code=404, detail="WEB_TOKEN 이 없습니다")
    return {"token": token}
