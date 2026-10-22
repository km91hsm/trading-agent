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
