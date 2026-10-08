"""LLM 오케스트레이터가 쓰는 도구 4개의 구현 (스펙 §1.1).

모든 함수는 예외 대신 {"ok": bool, ...} dict 를 돌려준다 (CLI 가 JSON 으로 그대로 출력).
순서 강제는 프롬프트가 아니라 여기의 전제조건이 담당한다:
  submit-decision ← 10_analyst_* 가 2개 이상 and 25_research_summary 존재.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from trader.agents import schemas
from trader.config import AccountConfig, Market, Settings
from trader.orchestrator import summary as summary_mod
from trader.orchestrator.run_store import (
    list_checkpoints,
    read_checkpoint,
    run_dir,
    write_checkpoint,
)
from trader.platform import paths

logger = logging.getLogger("trader.orchestrator")

_KST = ZoneInfo("Asia/Seoul")
ROUND_TRIP_COST_BP: dict[Market, int] = {"KR": 30, "US": 50}
MIN_REPORTS = 2
ANALYST_ROLES = ("technical", "fundamental", "news_macro", "sentiment")
_ANALYST_RE = re.compile(r"^10_analyst_(technical|fundamental|news_macro|sentiment)$")
_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
_STAGE_RE = re.compile(r"^\d{2}_[a-z0-9_\-]{1,40}$")
# LLM 이 checkpoint write 로 쓸 수 있는 단계. 30_decision 은 submit-decision 전용(전제조건 검증).
_LLM_WRITABLE = (_ANALYST_RE, _DEBATE_RE)


class ToolInputError(ValueError):
    """ticker·market·stage 형식 오류 (경로 탈출 방지)."""


def _validate_ids(ticker: str, market: str, stage: str | None = None) -> None:
    if not _TICKER_RE.match(ticker or ""):
        raise ToolInputError(f"ticker 형식 오류: {ticker!r}")
    if market not in ("KR", "US"):
        raise ToolInputError(f"market 은 KR 또는 US: {market!r}")
    if stage is not None and not _STAGE_RE.match(stage):
        raise ToolInputError(f"stage 형식 오류: {stage!r}")


def _unescape(obj: Any) -> Any:
    """Claude Code 가 서브에이전트 출력의 <·>·& 를 이스케이프하므로 문자열 값을 되돌린다."""
    if isinstance(obj, str):
        return html.unescape(obj)
    if isinstance(obj, dict):
        return {k: _unescape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unescape(v) for v in obj]
    return obj


def _guarded(fn):
    """ToolInputError 를 {"ok": false} 로 바꾸는 데코레이터."""
    import functools

    @functools.wraps(fn)
    def wrapper(ctx, *, ticker=None, market=None, stage=None, **kw):
        try:
            _validate_ids(ticker, market, stage)
        except ToolInputError as e:
            return {"ok": False, "error": str(e)}
        kwargs = {"ticker": ticker, "market": market, **kw}
        if stage is not None:
            kwargs["stage"] = stage
        return fn(ctx, **kwargs)

    return wrapper


class ToolError(RuntimeError):
    """도구 거부 (FAKE 모드·파이썬 호출자가 예외로 받고 싶을 때)."""


@dataclass
class ToolContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection | None = None
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _dir(ctx: ToolContext, ticker: str, market: Market, d: date) -> Path:
    return run_dir(d, market, ticker, ctx.state)


def _call(agent: str | None, meta: dict | None) -> dict | None:
    if agent is None and not meta:
        return None
    base = {"agent": agent or "unknown"}
    base.update({k: v for k, v in (meta or {}).items() if k in schemas.CallMeta.model_fields})
    return schemas.CallMeta.model_validate(base).model_dump()


def _wrap(data: Any, call: dict | None) -> dict:
    return {"schema_version": schemas.CHECKPOINT_SCHEMA_VERSION, "data": data, "call": call}


# --- checkpoint -------------------------------------------------------------


@_guarded
def checkpoint_list(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict:
    return {"ok": True, "stages": list_checkpoints(_dir(ctx, ticker, market, date))}


def trader_context(ctx: ToolContext, inputs: dict, market: Market) -> dict:
    """트레이더 입력의 파이썬 제공분. portfolio·memory·calibration 은 Phase 4·8 자리."""
    quote = ((inputs.get("technical") or {}).get("quote") or {}).get("data")
    return {
        "quote": quote,
        "risk_limits": ctx.settings.risk.model_dump(),
        "costs_bp": ROUND_TRIP_COST_BP[market],
        "portfolio": None,
        "memory": [],
        "calibration": None,
    }


# 서브에이전트에 넘기는 절은 Claude Code 의 Bash 출력 한도(약 30KB, 초과 시 파일로 치환돼
# LLM 이 못 본다)보다 훨씬 작아야 한다 (실기 확인).
# 지표가 250봉을 요약하므로 원시 봉은 최근 것만 준다.
COMPACT_LIMITS = {
    "daily": 30,
    "minute": 20,
    "orderbook_levels": 5,
    "news_stock": 15,
    "news_macro": 8,
    "news_summary_chars": 120,
    "posts": 20,
    "post_body_chars": 200,
    "post_title_chars": 100,
}


def _section_data(sec: Any) -> Any:
    return sec.get("data") if isinstance(sec, dict) else None


def _with_note(sec: dict, data: Any, note: str) -> dict:
    return {**sec, "data": data, "note": note}


def _trim_news(items: list | None, n: int, chars: int) -> list:
    out = []
    for it in (items or [])[:n]:
        if not isinstance(it, dict):
            continue
        row = {k: v for k, v in it.items() if k != "url"}
        if isinstance(row.get("summary"), str):
            row["summary"] = row["summary"][:chars]
        out.append(row)
    return out


def _compact_section(section: str, body: dict) -> dict:
    """역할별 절을 압축한다. 원본 체크포인트 파일은 바꾸지 않는다."""
    lim = COMPACT_LIMITS
    out = dict(body)
    if section == "technical":
        daily = _section_data(body.get("daily"))
        if isinstance(daily, list) and len(daily) > lim["daily"]:
            out["daily"] = _with_note(
                body["daily"],
                daily[-lim["daily"] :],
                f"최근 {lim['daily']}봉만 (전체 {len(daily)}봉은 indicators 에 반영됨)",
            )
        minute = _section_data(body.get("minute"))
        if isinstance(minute, list) and len(minute) > lim["minute"]:
            out["minute"] = _with_note(
                body["minute"], minute[-lim["minute"] :], f"최근 {lim['minute']}분봉만"
            )
        ob = _section_data(body.get("orderbook"))
        if isinstance(ob, dict):
            n = lim["orderbook_levels"]
            out["orderbook"] = _with_note(
                body["orderbook"],
                {**ob, "asks": (ob.get("asks") or [])[:n], "bids": (ob.get("bids") or [])[:n]},
                f"상위 {n}호가만",
            )
    elif section == "news_macro":
        news = _section_data(body.get("news"))
        if isinstance(news, dict):
            out["news"] = _with_note(
                body["news"],
                {
                    **news,
                    "stock": _trim_news(
                        news.get("stock"), lim["news_stock"], lim["news_summary_chars"]
                    ),
                    "macro": _trim_news(
                        news.get("macro"), lim["news_macro"], lim["news_summary_chars"]
                    ),
                },
                f"종목 {lim['news_stock']}건·거시 {lim['news_macro']}건, "
                f"요약 {lim['news_summary_chars']}자",
            )
    elif section == "sentiment":
        comm = _section_data(body.get("community"))
        if isinstance(comm, dict):
            posts = []
            for p in (comm.get("posts") or [])[: lim["posts"]]:
                if not isinstance(p, dict):
                    continue
                row = {k: v for k, v in p.items() if k != "url"}
                row["title"] = str(row.get("title", ""))[: lim["post_title_chars"]]
                row["body"] = str(row.get("body", ""))[: lim["post_body_chars"]]
                posts.append(row)
            out["community"] = _with_note(
                body["community"], {**comm, "posts": posts}, f"최근 {lim['posts']}건만"
            )
    return out


@_guarded
def checkpoint_read(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    stage: str,
    section: str | None = None,
) -> dict:
    """절(section)을 지정하면 서브에이전트용으로 압축해 돌려준다. 전체 읽기는 파일 그대로."""
    data = read_checkpoint(_dir(ctx, ticker, market, date), stage)
    if data is None:
        return {"ok": False, "error": f"체크포인트 없음: {stage}"}
    if section is None:
        return {"ok": True, "data": data}
    if stage == "00_inputs" and section == "trader_context":
        return {"ok": True, "data": trader_context(ctx, data, market)}
    body = data.get("data", data) if stage != "00_inputs" else data
    if section not in body:
        return {"ok": False, "error": f"절 없음: {section}"}
    if stage != "00_inputs":
        return {"ok": True, "data": {section: body[section]}}
    out = body[section]
    if isinstance(out, dict):
        out = _compact_section(section, out)
        errors = [e for e in data.get("errors", []) if e.startswith(section + ".")]
        out = {**out, "input_errors": errors, "meta": data.get("meta")}
    return {"ok": True, "data": out}


@_guarded
def checkpoint_write(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    stage: str,
    payload: dict,
    agent: str | None = None,
    call_meta: dict | None = None,
) -> dict:
    if stage.startswith("30_decision"):
        return {"ok": False, "error": "30_decision 은 submit-decision 도구로만 제출할 수 있습니다"}
    if not any(p.match(stage) for p in _LLM_WRITABLE):
        return {"ok": False, "error": f"알 수 없는 단계이거나 LLM 이 쓰는 단계가 아닙니다: {stage}"}
    model = schemas.schema_for(stage)
    if model is None:
        return {"ok": False, "error": f"알 수 없는 단계이거나 LLM 이 쓰는 단계가 아닙니다: {stage}"}
    try:
        validated = model.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패({stage}): {msgs}"}
    mismatch = _name_payload_mismatch(ctx, stage, ticker, validated)
    if mismatch:
        return {"ok": False, "error": mismatch}
    path = write_checkpoint(
        _dir(ctx, ticker, market, date),
        stage,
        _wrap(validated.model_dump(), _call(agent, call_meta)),
    )
    return {"ok": True, "path": str(path)}


def _name_payload_mismatch(ctx: ToolContext, stage: str, ticker: str, v: Any) -> str | None:
    m = _ANALYST_RE.match(stage)
    if m:
        if v.role != m.group(1):
            return f"파일명 role({m.group(1)}) 과 페이로드 role({v.role}) 불일치"
        if v.ticker != ticker:
            return f"ticker 불일치: {v.ticker} ≠ {ticker}"
        return None
    m = _DEBATE_RE.match(stage)
    if m:
        rnd, side = int(m.group(1)), m.group(2)
        if v.side != side or v.round != rnd:
            return f"파일명 r{rnd}_{side} 와 페이로드 round={v.round} side={v.side} 불일치"
        if rnd > ctx.settings.debate_rounds:
            return f"라운드 {rnd} 는 설정(debate_rounds={ctx.settings.debate_rounds})을 넘습니다"
    return None


def _load_turns(d: Path) -> dict[int, dict[str, schemas.DebateTurn]]:
    turns: dict[int, dict[str, schemas.DebateTurn]] = {}
    for stage in list_checkpoints(d):
        m = _DEBATE_RE.match(stage)
        if not m:
            continue
        data = read_checkpoint(d, stage) or {}
        turns.setdefault(int(m.group(1)), {})[m.group(2)] = schemas.DebateTurn.model_validate(
            data["data"]
        )
    return turns


@_guarded
def checkpoint_summarize(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict:
    """20_debate_* 를 모아 25_research_summary 를 만든다 (파이썬이 수렴 감시)."""
    d = _dir(ctx, ticker, market, date)
    turns = _load_turns(d)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
        return {"ok": False, "error": "라운드 0 의 bull·bear 턴이 모두 필요합니다"}
    final_round = max(r for r, sides in turns.items() if set(sides) == {"bull", "bear"})
    try:
        summ = summary_mod.research_summary(turns[0], turns[final_round], rounds=final_round)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = write_checkpoint(d, "25_research_summary", _wrap(summ, None))
    return {
        "ok": True,
        "path": str(path),
        "sycophantic": summ["sycophantic"],
        "preferred": summ["preferred"],
    }


# --- submit-decision --------------------------------------------------------


@_guarded
def submit_decision(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    payload: dict,
    agent: str = "trader",
    call_meta: dict | None = None,
) -> dict:
    d = _dir(ctx, ticker, market, date)
    stages = list_checkpoints(d)
    reports = [s for s in stages if _ANALYST_RE.match(s)]
    missing: list[str] = []
    if len(reports) < MIN_REPORTS:
        missing.append(f"10_analyst_* {len(reports)}개 (최소 {MIN_REPORTS})")
    if "25_research_summary" not in stages:
        missing.append("25_research_summary 없음")
    if missing:
        return {"ok": False, "error": "전제조건 미충족: " + ", ".join(missing)}
    try:
        decision = schemas.Decision.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(30_decision): {msgs}"}
    if decision.ticker != ticker:
        return {"ok": False, "error": f"ticker 불일치: {decision.ticker} ≠ {ticker}"}
    path = write_checkpoint(d, "30_decision", _wrap(decision.model_dump(), _call(agent, call_meta)))
    # Phase 4 전까지 리스크 게이트·주문은 자리만 둔다 (결정은 1회, 판정은 계좌마다).
    for a in ctx.accounts:
        if market in a.markets:
            write_checkpoint(
                d,
                f"40_risk_check_{a.id}",
                _wrap(
                    {
                        "account_id": a.id,
                        "approved": None,
                        "adjusted_qty": None,
                        "reason": "phase4",
                    },
                    None,
                ),
            )
    logger.info("결정 저장 %s %s: %s", market, ticker, decision.action)
    return {"ok": True, "path": str(path), "action": decision.action, "risk": "phase4"}


# --- status -----------------------------------------------------------------


def sessions_today(conn: sqlite3.Connection | None, now: datetime) -> int:
    if conn is None:
        return 0
    start = now.astimezone(_KST).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_calls WHERE agent='orchestrator' "
        "AND COALESCE(model, '') != 'fake' AND created_at >= ?",
        (start.astimezone(UTC).isoformat(),),
    ).fetchone()
    return int(row["n"])


def status(ctx: ToolContext, *, market: Market, date: date) -> dict:
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    base = ctx.state if ctx.state is not None else paths.state_dir()
    market_dir = base / "runs" / date.isoformat() / market
    tickers: dict[str, list[str]] = {}
    if market_dir.exists():
        for p in sorted(market_dir.iterdir()):
            if p.is_dir():
                tickers[p.name] = list_checkpoints(p)
    return {
        "ok": True,
        "date": date.isoformat(),
        "market": market,
        "tickers": tickers,
        "sessions_today": sessions_today(ctx.conn, ctx.now()),
        "cap": ctx.settings.llm.daily_session_cap,
    }
