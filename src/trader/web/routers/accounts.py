"""accounts: 계좌 목록 (실효 모드·성향·시장별 NAV·미승인 수). 계좌번호는 내보내지 않는다."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Request

from trader.ledger import views
from trader.orchestrator.executor import effective_mode
from trader.web.routers._common import deps_of, today_kst

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
def list_accounts(request: Request) -> list[dict]:
    deps = deps_of(request)
    today = today_kst(request)
    out = []
    with closing(deps.conn_factory()) as conn:
        for a in deps.all_accounts:
            mode, note = effective_mode(conn, a)  # 실전 auto 는 확인 기록 없으면 off 로 강등
            out.append(
                {
                    "id": a.id,
                    "env": a.env,
                    "markets": list(a.markets),
                    "mode": mode,
                    "mode_note": note,
                    "yaml_mode": a.mode,
                    **views.account_summary(conn, a.id, today),
                    "asset": a.asset,  # 설정이 출처(원장 accounts.asset 은 사본)
                }
            )
    return out
