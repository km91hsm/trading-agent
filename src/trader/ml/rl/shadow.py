"""섀도/적용 (스펙 §9.4 "모의 한정 섀도"). executor 가 **모의 계좌·BUY/ADD** 에만 부른다.

- shadow: 제안 비중을 `rl_shadow` 에 기록만 한다.
- apply: 제안 비중으로 target_weight 를 바꿔 리스크 게이트에 넘긴다(한도는 게이트가 그대로 강제).
실전 계좌는 어떤 경로로도 이 모듈을 거치지 않는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

from trader.agents.schemas import Decision
from trader.config import MlConfig, RlSizerCfg
from trader.memory import vector
from trader.ml import quant_report, registry
from trader.ml.rl.policy import Policy
from trader.risk.rules import Portfolio

logger = logging.getLogger("trader.ml")
NAME = "rl_sizer"


@dataclass
class Sizer:
    policy: Policy
    version: str
    cfg: RlSizerCfg


def load(cfg: MlConfig, state: Path | None) -> Sizer | None:
    """enabled 이고 승격된 최신 모델이 있을 때만. 로드 실패는 경고 후 None."""
    rl = cfg.models.rl_sizer
    if not rl.enabled:
        return None
    try:
        got = registry.latest(NAME, state=state)
    except Exception as e:  # noqa: BLE001 - 레지스트리 문제가 결정 실행을 막지 않는다
        logger.warning("rl_sizer 로드 실패: %s", e)
        return None
    if got is None:
        return None
    policy, meta = got
    return Sizer(policy=policy, version=str(meta.get("version") or "?"), cfg=rl)


def base_observation(
    state: Path | None, *, ticker: str, market: str, decision: Decision
) -> list[float]:
    """스텝 관측 14차원: 피처 스토어 최신 행의 상황 벡터 + [confidence, target_weight]."""
    row = quant_report.latest_row(ticker, market, state) or {}
    return [
        *vector.situation_vector(row, market),
        float(decision.confidence),
        float(decision.target_weight),
    ]


def observe(
    state: Path | None,
    *,
    ticker: str,
    market: str,
    decision: Decision,
    portfolio: Portfolio,
    nav_hist: list[tuple[str, Decimal]],
    base: list[float] | None = None,
) -> list[float]:
    """16차원 관측 = base 14 + [현재 비중, 드로다운(최근 NAV 피크 대비)]."""
    if base is None:
        base = base_observation(state, ticker=ticker, market=market, decision=decision)
    held = portfolio.positions.get(ticker)
    pos_w = float(held.eval_amount / portfolio.nav) if held and portfolio.nav > 0 else 0.0
    navs = [Decimal(str(v)) for _, v in nav_hist]
    peak = max(navs) if navs else Decimal(0)
    dd = float(navs[-1] / peak - 1) if len(navs) >= 2 and peak > 0 else 0.0
    return [*base, pos_w, dd]


def propose(sizer: Sizer, obs: list[float]) -> float:
    w = float(sizer.policy.act(np.asarray(obs, dtype="float64")))
    return min(max(w, 0.0), sizer.cfg.max_weight)


def record(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    account_id: str,
    model_version: str,
    obs: list[float],
    llm_weight: float,
    proposed_weight: float,
    applied: bool,
) -> int:
    """결정·계좌당 1행 (재실행에도 처음 기록을 유지)."""
    conn.execute(
        "INSERT OR IGNORE INTO rl_shadow(decision_id, account_id, created_at, model_version, "
        "obs, llm_weight, proposed_weight, applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            account_id,
            datetime.now(UTC).isoformat(),
            model_version,
            json.dumps([float(x) for x in obs]),
            float(llm_weight),
            float(proposed_weight),
            1 if applied else 0,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rl_shadow WHERE decision_id=? AND account_id=?", (decision_id, account_id)
    ).fetchone()
    return int(row["id"])


def recent(conn: sqlite3.Connection, account_id: str, *, days: int = 30) -> list[dict]:
    """최근 days 일의 섀도 행(최신 먼저) + 만기 결과(price_return)로 가상 실현수익."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT s.id, s.decision_id, s.account_id, s.created_at, s.model_version, s.llm_weight, "
        "s.proposed_weight, s.applied, d.ticker, d.market, d.date AS decision_date, "
        "m.price_return FROM rl_shadow s "
        "LEFT JOIN decisions d ON d.id = s.decision_id "
        "LEFT JOIN memory_entries m ON m.decision_id = s.decision_id "
        "WHERE s.account_id=? AND s.created_at >= ? ORDER BY s.created_at DESC, s.id DESC",
        (account_id, since),
    ).fetchall()
    out = []
    for r in rows:
        pr = r["price_return"]
        out.append(
            {
                "id": r["id"],
                "decision_id": r["decision_id"],
                "account_id": r["account_id"],
                "created_at": r["created_at"],
                "model_version": r["model_version"],
                "ticker": r["ticker"],
                "market": r["market"],
                "decision_date": r["decision_date"],
                "llm_weight": r["llm_weight"],
                "proposed_weight": r["proposed_weight"],
                "applied": bool(r["applied"]),
                "price_return": pr,
                "realized_rl": None if pr is None else float(r["proposed_weight"]) * float(pr),
                "realized_llm": None if pr is None else float(r["llm_weight"]) * float(pr),
            }
        )
    return out
