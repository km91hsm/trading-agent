"""performance: 계좌·시장별 성과 리포트 (스펙 §13)."""

from __future__ import annotations

import threading
import time
from contextlib import closing

from fastapi import APIRouter, Query, Request

from trader.ledger import performance, shadow
from trader.ml import features
from trader.web.routers._common import deps_of, require_account, require_market

router = APIRouter(prefix="/performance", tags=["performance"])

CACHE_TTL_S = 300.0
# (account, market, days) → (version, saved_at, report). 리포트는 joblib 모델 + parquet 60개를
# 읽어 수백 ms 라, SSE change 마다(대시보드 2건) 다시 만들면 데몬·세션의 원장 쓰기와 경합한다.
_CACHE: dict[tuple[str, str, int], tuple[tuple, float, dict]] = {}
_LOCK = threading.Lock()


def _version(conn, account_id: str, market: str, state) -> tuple:
    """리포트 입력이 바뀌었는지 보는 값: 스냅샷·체결·주문 수와 피처 디렉터리 mtime."""
    snap = conn.execute(
        "SELECT COUNT(*) AS n, MAX(id) AS m FROM positions_snapshot "
        "WHERE account_id=? AND market=?",
        (account_id, market),
    ).fetchone()
    fills = conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE account_id=?", (account_id,)
    ).fetchone()["n"]
    orders = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE account_id=?", (account_id,)
    ).fetchone()["n"]
    return (snap["n"], snap["m"], fills, orders, features.store_mtime(state))


def cached_report(conn, state, account_id: str, market: str, days: int) -> dict:
    key = (account_id, market, days)
    version = _version(conn, account_id, market, state)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == version and now - hit[1] < CACHE_TTL_S:
            return hit[2]
    report = performance.report(conn, state, account_id, market, days=days)
    # 리포트가 피처 디렉터리를 만들 수 있어(mtime 변화) 만든 **뒤**의 버전을 기억한다
    with _LOCK:
        _CACHE[key] = (_version(conn, account_id, market, state), now, report)
    return report


@router.get("")
def performance_report(
    request: Request,
    account_id: str | None = None,
    market: str | None = None,
    days: int = Query(30, ge=1, le=3650),
) -> dict:
    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        return cached_report(conn, deps.state, acct.id, m, days)


@router.get("/shadow")
def shadow_report(request: Request, market: str | None = None) -> dict:
    """섀도 포트폴리오(전체 검토 L1): LLM 선택 − 같은 날 후보 1/N·정량 상위의 만기 쌍 통계.
    계좌와 무관한 시장 단위 비교라 account_id 를 받지 않는다."""
    from trader import readiness

    m = (market or "KR").upper()
    if m not in ("KR", "US"):
        m = "KR"
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        pairs = {
            b: shadow.paired_stats(conn, b=b, market=m)
            for b in ("candidates_1n", "quant_topn", "index_hold", "index_trend")
        }
        cash = shadow.cash_effect(conn, market=m)
        by_date: dict[str, dict] = {}
        for r in shadow.rows(conn, market=m):
            by_date.setdefault(r["date"], {"date": r["date"]})[r["strategy"]] = r["fwd_ret"]
    recent = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)[:10]
    return {
        "market": m,
        "min_pairs": readiness.MIN_SHADOW_PAIRS,
        "pairs": pairs,
        "cash_effect": cash,  # LLM 이 현금인 날의 0 − 후보 1/N(2-L3, 선택 효과와 별개)
        "recent": recent,
    }
