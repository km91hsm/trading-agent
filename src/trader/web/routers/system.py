"""system: 헬스·STATUS.md·설정 요약 (시크릿 없음)."""

from __future__ import annotations

import threading
import time
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
        # 토큰 없는 조회 API 라 tailnet 사용자 이메일·호스트명은 수만(2-W7)
        "web": {
            "bind": s.web.bind,
            "port": s.web.port,
            "realtime_ws": s.web.realtime_ws,
            "tailscale_hosts_count": len(s.web.tailscale_hosts),
            "tailscale_users_count": len(s.web.tailscale_users),
        },
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
        enabled, source = usage.throttle_enabled(conn, deps.settings)
    t = deps.settings.llm.usage_throttle
    return {
        "rate_limits": limits,
        "throttle": {
            "enabled": enabled,
            "source": source,  # control(설정 화면) | yaml
            "soft_pct": t.soft_pct,
            "hard_pct": t.hard_pct,
            "state": "ok" if ok_new else ("soft" if ok_re else "hard"),
            "reason": why_new or why_re,
        },
        "usage_file_present": path is not None and path.is_file(),  # 홈 경로는 안 돌려줌(2-W7)
        "windows": windows,
        "note": USAGE_NOTE,
    }


USAGE_ENSURE_MIN_INTERVAL_S = 60.0  # 페이지 접속 확인 요청 최소 간격(새로고침이 몰려도 haiku 1회)
_USAGE_ENSURE_LAST = [0.0]
_USAGE_ENSURE_LOCK = threading.Lock()


@router.post("/usage/ensure-fresh")
def usage_ensure_fresh(request: Request) -> dict:
    """페이지 접속·새로고침 때(토큰 없음): 최신 관측이 `usage_probe.skip_if_recent_min` 보다
    오래됐고 데몬이 살아 있으면 대기열에 `usage_probe` 를 넣는다(실행은 데몬, haiku 단발 1회).
    60초에 한 번만 실제로 넣고, 이미 대기·실행 중이면 그대로 둔다. 결과는 SSE `usage` 로 반영."""
    from trader import jobs as jobq
    from trader.web.routers._common import require_same_origin
    from trader.web.routers.jobs import daemon_alive

    require_same_origin(request)
    deps = request.app.state.deps
    cfg = deps.settings.llm.usage_probe
    if not cfg.enabled:
        return {"skipped": "꺼짐"}
    now = deps.now()
    with closing(deps.conn_factory()) as conn:
        obs = usage.latest_observation(conn, now, usage.usage_file(deps.settings.llm.usage_file))
        age = (obs or {}).get("age_s")
        if age is not None and age < cfg.skip_if_recent_min * 60:
            return {"fresh": True, "age_s": age}
        if not daemon_alive(request):
            return {"skipped": "데몬 꺼짐"}
        with _USAGE_ENSURE_LOCK:
            if time.monotonic() - _USAGE_ENSURE_LAST[0] < USAGE_ENSURE_MIN_INTERVAL_S:
                return {"throttled": True}
            _USAGE_ENSURE_LAST[0] = time.monotonic()
        try:
            rid = jobq.enqueue(conn, "usage_probe", {}, source="web", now=now)
        except jobq.DuplicateRequest:
            return {"queued": True, "already": True}
    return {"queued": True, "id": rid}


@router.get("/events")
def events_status(request: Request, market: str = "KR") -> dict:
    """사건 수혜(2026-10-27): 오늘 감지된 사건(데몬 뉴스 수집·scan 이 남긴
    `control.events:<시장>`)과 표의 사건 목록. 토큰 없음. 감지된 것이 없으면 events []."""
    from trader.config import load_events
    from trader.control import control
    from trader.scan import events

    if market not in ("KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR|US")
    deps = request.app.state.deps
    try:
        cfg = load_events()
        table = [
            {
                "id": e.id,
                "label": e.label,
                "kind": e.kind,
                "n": len(e.beneficiaries.get(market, [])),
            }
            for e in cfg.events
            if e.beneficiaries.get(market)
        ]
        enabled = cfg.enabled
    except Exception:  # noqa: BLE001 - 표 오류는 화면에 '표 없음'
        table, enabled = [], False
    with closing(deps.conn_factory()) as conn:
        st = control.get_flag(conn, f"{events.FLAG}:{market}") or {}
    return {
        "enabled": enabled,
        "market": market,
        "date": st.get("date"),
        "events": st.get("events") or [],
        "table": table,
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
            build_jobs(
                deps.schedule, deps.settings, markets=list(deps.settings.markets),
                fo=bool(deps.fo_accounts),
            )
            if deps.schedule is not None
            else []
        )  # fmt: skip
        result = health.check(
            conn,
            deps.settings,
            deps.all_accounts,  # 데몬 ops_check 와 같은 범위(파생 포함)
            now=deps.now(),
            state=deps.state,
            schedule_jobs=jobs,
        )
    return result | {"headline": health.headline(result), "source": "live"}


@router.get("/saveticker")
def saveticker_status(request: Request) -> dict:
    """세이브티커 수집기 상태(설정 화면): 계정 파일 유무·서비스 상태·멈춘 이유·작업별 상태·쉬는
    작업·마지막 수집 시각. 계정·값은 돌려주지 않는다."""
    from trader import saveticker as st
    from trader.saveticker import automation

    home = st.home()
    return {"configured": st.credentials_path().is_file(), **automation.status(home)}


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
            "pending": sorted((st.get("pending") or {}).keys()),  # 판정 대기(Phase 25)
            "judge": scalp_engine.judge_summary(deps.state, market=m, days=[d], cfg=cfg),
        }
    return {
        "enabled": cfg.enabled,
        "judge": cfg.judge,
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
        build_jobs(
            deps.schedule, deps.settings, markets=list(deps.settings.markets),
            fo=bool(deps.fo_accounts),
        )
        if deps.schedule is not None
        else []
    )  # fmt: skip
    return heartbeat.read_status(base, deps.now(), jobs)
