"""system: 헬스·STATUS.md·설정 요약 (시크릿 없음)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request

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
