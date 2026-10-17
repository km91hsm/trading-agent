"""비교 리포트 (스펙 §11 Phase 9 완료 기준 "8주 워크포워드 비교"): 모델 요약 + 섀도 실적."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.ml import registry
from trader.ml.rl import shadow

NAME = "rl_sizer"
MODEL_KEYS = (
    "version",
    "saved_at",
    "backend",
    "source",
    "steps",
    "seeds",
    "promoted",
    "promote_reason",
    "summary",
)


def model_summary(meta: dict) -> dict:
    return {k: meta.get(k) for k in MODEL_KEYS}


def current_model(state: Path | None) -> dict | None:
    """승격 최신, 없으면 최신."""
    metas = registry.list_models(NAME, state)
    if not metas:
        return None
    return next((m for m in metas if m.get("promoted")), metas[0])


def comparison(
    conn: sqlite3.Connection,
    state: Path | None,
    *,
    account_id: str,
    market: str | None = None,
    weeks: int = 8,
) -> dict:
    meta = current_model(state)
    rows = shadow.recent(conn, account_id, days=weeks * 7)
    if market:
        rows = [r for r in rows if r.get("market") == market]
    matured = [r for r in rows if r["price_return"] is not None]
    n = len(rows)
    return {
        "weeks": weeks,
        "account_id": account_id,
        "market": market,
        "model": model_summary(meta) if meta else None,
        "shadow": {
            "n": n,
            "applied": sum(1 for r in rows if r["applied"]),
            "avg_llm_weight": (sum(r["llm_weight"] for r in rows) / n) if n else None,
            "avg_proposed_weight": (sum(r["proposed_weight"] for r in rows) / n) if n else None,
            "matured": len(matured),
            "realized_rl_sum": sum(r["realized_rl"] for r in matured) if matured else None,
            "realized_llm_sum": sum(r["realized_llm"] for r in matured) if matured else None,
        },
    }
