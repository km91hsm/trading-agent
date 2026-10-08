"""LLM 오케스트레이터가 쓰는 도구 4개의 구현 (스펙 §1.1).

모든 함수는 예외 대신 {"ok": bool, ...} dict 를 돌려준다 (CLI 가 JSON 으로 그대로 출력).
순서 강제는 프롬프트가 아니라 여기의 전제조건이 담당한다:
  submit-decision ← 10_analyst_* 가 2개 이상 and 25_research_summary 존재.
"""

from __future__ import annotations

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

logger = logging.getLogger("trader.orchestrator")

_KST = ZoneInfo("Asia/Seoul")
ROUND_TRIP_COST_BP: dict[Market, int] = {"KR": 30, "US": 50}
MIN_REPORTS = 2
_ANALYST_RE = re.compile(r"^10_analyst_")
_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")


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


def checkpoint_read(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    stage: str,
    section: str | None = None,
) -> dict:
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
    out = {section: body[section]} if stage != "00_inputs" else body[section]
    if stage == "00_inputs":
        errors = [e for e in data.get("errors", []) if e.startswith(section + ".")]
        if isinstance(out, dict):
            out = {**out, "input_errors": errors, "meta": data.get("meta")}
    return {"ok": True, "data": out}


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
    model = schemas.schema_for(stage)
    if model is None:
        return {"ok": False, "error": f"알 수 없는 단계이거나 LLM 이 쓰는 단계가 아닙니다: {stage}"}
    try:
        validated = model.model_validate(payload)
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패({stage}): {msgs}"}
    path = write_checkpoint(
        _dir(ctx, ticker, market, date),
        stage,
        _wrap(validated.model_dump(), _call(agent, call_meta)),
    )
    return {"ok": True, "path": str(path)}


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
        decision = schemas.Decision.model_validate(payload)
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
        "SELECT COUNT(*) AS n FROM agent_calls WHERE agent='orchestrator' AND created_at >= ?",
        (start.astimezone(UTC).isoformat(),),
    ).fetchone()
    return int(row["n"])


def status(ctx: ToolContext, *, market: Market, date: date) -> dict:
    base = ctx.state if ctx.state is not None else run_dir(date, market, "_", None).parents[0]
    market_dir = (base / "runs" / date.isoformat() / market) if ctx.state else base
    tickers: dict[str, list[str]] = {}
    if market_dir.exists():
        for p in sorted(market_dir.iterdir()):
            if p.is_dir() and p.name != "_":
                tickers[p.name] = list_checkpoints(p)
    return {
        "ok": True,
        "date": date.isoformat(),
        "market": market,
        "tickers": tickers,
        "sessions_today": sessions_today(ctx.conn, ctx.now()),
        "cap": ctx.settings.llm.daily_session_cap,
    }
