"""system: 헬스·STATUS.md·설정 요약 (시크릿 없음)."""

from __future__ import annotations

from contextlib import closing
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, Request

from trader import heartbeat, usage
from trader.control import control
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


USAGE_NOTE = (
    "세션마다 claude 가 알려 준 구독 사용률(또는 대화형 상태줄 관측) 중 최신값. "
    "정확한 값은 claude 에서 /usage"
)


@router.get("/usage")
def usage_view(request: Request) -> dict:
    """Claude 사용량: 최신 관측(세션 rate_limit_event 원장 ↔ 상태줄 파일, Phase 24) + 감속 상태
    + 이 저장소 세션의 5시간·7일 창 집계."""
    deps = request.app.state.deps
    now = deps.now()
    path = usage.usage_file(deps.settings.llm.usage_file)
    with closing(deps.conn_factory()) as conn:
        limits = usage.latest_observation(conn, now, path)
        windows = usage.ledger_windows(conn, now, cap=deps.settings.llm.daily_session_cap)
        ok_new, why_new = usage.session_allowance(conn, deps.settings, kind="analyze", now=now)
        ok_re, why_re = usage.session_allowance(conn, deps.settings, kind="resume", now=now)
    t = deps.settings.llm.usage_throttle
    return {
        "rate_limits": limits,
        "throttle": {
            "enabled": t.enabled,
            "soft_pct": t.soft_pct,
            "hard_pct": t.hard_pct,
            "state": "ok" if ok_new else ("soft" if ok_re else "hard"),
            "reason": why_new or why_re,
        },
        "usage_file": str(path) if path is not None else None,
        "windows": windows,
        "note": USAGE_NOTE,
    }


@router.get("/theme")
def theme_status(request: Request, market: str = "KR") -> dict:
    """오늘 테마 열기 결과(Phase 25, 데몬 <시장>_theme). 토큰 없음. 결과가 없으면 themes []."""
    from trader.control import control
    from trader.scan import theme

    if market not in ("KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR|US")
    deps = request.app.state.deps
    with closing(deps.conn_factory()) as conn:
        heat = control.get_flag(conn, f"{theme.FLAG}:{market}") or {}
    return {
        "enabled": deps.settings.theme.enabled,
        "top_n": deps.settings.theme.top_n,
        "market": market,
        "date": heat.get("date"),
        "observed_at": heat.get("observed_at"),
        "themes": heat.get("themes") or [],
        "signals": heat.get("signals") or {},
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


@router.get("/ops")
def ops_status(request: Request, live: bool = False) -> dict:
    """운영 점검(Phase 23 A). 기본은 데몬 `ops_check` 의 마지막 결과(`control.ops_result`),
    `live=true` 면 지금 다시 판정(읽기 전용, 기록 없음). 토큰 없음."""
    from trader.daemon import build_jobs
    from trader.ops import health

    deps = request.app.state.deps
    with closing(deps.conn_factory()) as conn:
        if not live:
            cached = control.get_flag(conn, health.RESULT_FLAG)
            if cached:
                return cached | {"headline": health.headline(cached), "source": "daemon"}
        jobs = (
            build_jobs(deps.schedule, deps.settings, markets=list(deps.settings.markets))
            if deps.schedule is not None
            else []
        )
        result = health.check(
            conn,
            deps.settings,
            deps.accounts,
            now=deps.now(),
            state=deps.state,
            schedule_jobs=jobs,
        )
    return result | {"headline": health.headline(result), "source": "live"}


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
