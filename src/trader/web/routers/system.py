"""system: 헬스·STATUS.md·설정 요약 (시크릿 없음)."""

from __future__ import annotations

from contextlib import closing
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request

from trader import heartbeat, usage
from trader.platform import paths

router = APIRouter(prefix="/system", tags=["system"])


def _version() -> str:
    try:
        return version("trader")
    except PackageNotFoundError:
        return "dev"


@router.get("/health")
def health(request: Request) -> dict:
    deps = request.app.state.deps
    return {"ok": True, "version": _version(), "now": deps.now().isoformat()}


@router.get("/status")
def status(request: Request) -> dict:
    deps = request.app.state.deps
    base = deps.state if deps.state is not None else paths.state_dir()
    p = base / "STATUS.md"
    return {"markdown": p.read_text(encoding="utf-8") if p.is_file() else ""}


@router.get("/settings")
def settings(request: Request) -> dict:
    s = request.app.state.deps.settings
    return {
        "markets": list(s.markets),
        "risk": s.risk.model_dump(),
        "debate_rounds": s.debate_rounds,
        "models": dict(s.models),
        "web": s.web.model_dump(),
        "llm": {"budget_usd": s.llm.budget_usd, "daily_session_cap": s.llm.daily_session_cap},
    }


USAGE_NOTE = "정확한 구독 사용률은 claude 에서 /usage 로 확인. 여기 값은 마지막 관측값"


@router.get("/usage")
def usage_view(request: Request) -> dict:
    """Claude 사용량: 상태줄 관측 파일(있으면) + 이 저장소 세션의 5시간·7일 창 집계."""
    deps = request.app.state.deps
    now = deps.now()
    path = usage.usage_file(deps.settings.llm.usage_file)
    limits = usage.read_rate_limits(path, now) if path is not None else None
    with closing(deps.conn_factory()) as conn:
        windows = usage.ledger_windows(conn, now, cap=deps.settings.llm.daily_session_cap)
    return {
        "rate_limits": limits,
        "usage_file": str(path) if path is not None else None,
        "windows": windows,
        "note": USAGE_NOTE,
    }


@router.get("/regime")
def regime_status(request: Request) -> dict:
    """시장별 국면(Phase 17 H) + 헤지 설정 요약. 토큰 없음."""
    from trader.risk import regime as regime_mod

    deps = request.app.state.deps
    h = deps.settings.hedge
    with closing(deps.conn_factory()) as conn:
        markets = regime_mod.summary(conn, list(deps.settings.markets))
    return {
        "enabled": h.enabled,
        "max_weight_pct": h.max_weight_pct,
        "leverage": h.leverage,
        "bear_min_signals": h.bear_min_signals,
        "market_session": h.market_session,
        "min_weight_pct": h.min_weight_pct,
        "instruments": {
            m: {k: v.model_dump() for k, v in d.items()} for m, d in h.instruments.items()
        },
        "markets": markets,
    }


@router.get("/scalp")
def scalp_status(request: Request, market: str | None = None) -> dict:
    """분 단위 단타(Phase 23 S) 상태: 설정·오늘 진입/청산·중단 사유. 토큰 없음."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine as scalp_engine

    deps = request.app.state.deps
    cfg = deps.settings.scalp
    markets = [market] if market else list(deps.settings.markets)
    today_by = {}
    for m in markets:
        d = run_date_for(m, deps.now())
        st = scalp_engine.load_state(deps.state, d, m)
        today_by[m] = {
            "date": d.isoformat(),
            "open": st.get("open") or [],
            "closed": st.get("closed") or [],
            "halted": st.get("halted"),
        }
    return {
        "enabled": cfg.enabled,
        "max_weight_pct": cfg.max_weight_pct,
        "max_positions": cfg.max_positions,
        "markets": today_by,
    }


@router.get("/daemon")
def daemon_status(request: Request) -> dict:
    """데몬 생존(daemon.lock pid)·하트비트(잡별 마지막 실행·다음 예정)·잡 예정표."""
    from trader.daemon import build_jobs

    deps = request.app.state.deps
    base = deps.state if deps.state is not None else paths.state_dir()
    jobs = (
        build_jobs(deps.schedule, deps.settings, markets=list(deps.settings.markets))
        if deps.schedule is not None
        else []
    )
    return heartbeat.read_status(base, deps.now(), jobs)
