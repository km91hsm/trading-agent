"""파생 세션 실행(Phase 26 단계 3): 입력 수집·저장 → `kind="fo"` 오케스트레이터 세션 1회 →
`_fo_decision` 읽기. 데몬 잡 `kr_fo_session`(단계 4)·CLI `trader fo session` 이 같은 함수를
부른다. 세션이 결정·실행을 맡는 게 아니라 **도구**(`submit-fo-decision`)가 게이트·실행을 맡고
여기는 세션 앞뒤의 파이썬 몫만 둔다: 상한(`fo.session_cap`), 오늘 결정 멱등, `fo.enabled=false`
∧ 보유 없음이면 세션 생략(비용), 과거 asof(`meta.historical`)는 dry-run 강제."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trader.config import AccountConfig, Settings
from trader.fo import inputs as inputs_mod
from trader.fo import store
from trader.fo.executor import FoExecContext
from trader.orchestrator import run_store, tools
from trader.orchestrator import session as session_mod
from trader.orchestrator.run_store import read_checkpoint

log = logging.getLogger("trader.fo")

MARKET = inputs_mod.MARKET
DECISION_STAGE = "_fo_decision"


def _decision(today: date, state: Path | None) -> dict | None:
    cp = read_checkpoint(run_store.market_dir(today, MARKET, state), DECISION_STAGE)
    data = cp.get("data") if isinstance(cp, dict) else None
    return data if isinstance(data, dict) else None


def run(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    fo_accounts: list[AccountConfig],
    brokers: dict[str, Any],
    market_client: Any | None,
    master: list,
    date: date,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    dry_run: bool = False,
    state: Path | None = None,
    session_runner: Callable[..., Any] | None = None,
    notifier: Any = None,
    cancel: Any | None = None,
    asof: datetime | None = None,
) -> dict:
    """{inputs_path, session, decision, skipped, dry_run, forced_dry_run}. `skipped` 가 있으면
    세션을 띄우지 않은 사유(오늘 결정 있음·상한·계좌 없음·enabled=false∧보유 없음)."""
    out: dict[str, Any] = {
        "inputs_path": None, "session": None, "decision": None, "skipped": None,
        "dry_run": dry_run, "forced_dry_run": False,
    }  # fmt: skip
    accounts = [a for a in fo_accounts if a.asset == "fo"]
    if not accounts:
        out["skipped"] = "asset: fo 계좌 없음"
        return out
    existing = _decision(date, state)
    if existing is not None:
        out["decision"] = existing
        out["skipped"] = f"오늘 {DECISION_STAGE} 결정이 이미 있음(세션 생략)"
        return out
    if not settings.fo.enabled and not any(store.open_positions(conn, a.id) for a in accounts):
        out["skipped"] = "fo.enabled=false ∧ 보유 포지션 없음(세션 생략)"
        return out
    cap = settings.fo.session_cap
    if tools.sessions_today(conn, now(), kind="fo") >= cap:
        out["skipped"] = f"오늘 fo 세션 상한({cap}) 도달"
        return out
    at = asof or now()
    ctx = FoExecContext(
        settings=settings, accounts=accounts, conn=conn, brokers=brokers,
        market_client=market_client, master=master, state=state, now=now, dry_run=dry_run,
        **({"notifier": notifier} if notifier is not None else {}),
    )  # fmt: skip
    inputs = inputs_mod.build_inputs(ctx, today=date, asof=at, accounts=accounts)
    historical = bool((inputs.get("meta") or {}).get("historical"))
    ctx.historical = historical
    if historical and not dry_run:
        log.warning(
            "파생 세션 asof %s 가 max_input_age_s(%s초)보다 과거 — dry_run 강제(주문 없음)",
            at.isoformat(), settings.fo.max_input_age_s,
        )  # fmt: skip
        dry_run = True
        out["forced_dry_run"] = True
        out["dry_run"] = True
        ctx.dry_run = True
    path = inputs_mod.save_inputs(inputs, today=date, state=state)
    out["inputs_path"] = str(path)
    if inputs.get("errors"):
        log.info("파생 입력 오류 절 %d건: %s", len(inputs["errors"]), sorted(inputs["errors"]))
    runner = session_runner or session_mod.run_session
    spec = session_mod.SessionSpec(
        market=MARKET, date=date, tickers=[], dry_run=dry_run, asof=at, kind="fo"
    )
    try:
        extra = {"cancel": cancel} if cancel is not None else {}
        res = runner(
            spec, settings, conn, [], state=state, now=now, brokers=brokers, notifier=notifier,
            fo=ctx, **extra,
        )  # fmt: skip
    except session_mod.PipelineError as e:
        log.warning("파생 세션 생략: %s", e)
        out["skipped"] = str(e)
        return out
    out["session"] = {
        "ok": res.ok, "fake": res.fake, "cost_usd": res.cost_usd, "error": res.error,
        "stages_written": res.stages_written,
    }  # fmt: skip
    out["decision"] = _decision(date, state)
    if not res.ok or out["decision"] is None:
        log.warning("파생 세션 실패 또는 결정 없음: %s", res.error)
        out["error"] = res.error or "결정 없음"
    return out
