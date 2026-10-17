"""신호 적중률 보정 (스펙 §9.3).

- signal_hits: analyst_reports(신호) × memory_entries(결과) 조인. 신호 BUY/STRONG_BUY → +1,
  SELL/STRONG_SELL → −1, 그 외 0. 결과 부호 = sign(realized_return 의 방향 무관 원수익).
- hit_rates: 역할별 적중률, 질의 벡터 코사인 이웃 k 개 적중률, bias(평균 신호 − 평균 결과 부호).
- build_calibration: 결과 엔트리 수 < min_decisions 면 None (calibration_runs 에 비활성 기록).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trader.agents import schemas
from trader.memory import vector as vec

_KST = ZoneInfo("Asia/Seoul")
SIGNAL_SIGN = {"STRONG_BUY": 1, "BUY": 1, "SELL": -1, "STRONG_SELL": -1}
DECISION_SIGN = {"BUY": 1.0, "ADD": 1.0, "SELL": -1.0, "TRIM": -1.0, "HOLD": 0.0}
FLAT_BAND = 0.0


def _price_sign(entry_realized: float, action: str) -> int:
    """memory 의 실현수익은 결정 방향 기준이므로 원래 가격 방향으로 되돌린다."""
    s = DECISION_SIGN.get(action, 0.0)
    raw = entry_realized * s if s else entry_realized
    if raw > FLAT_BAND:
        return 1
    if raw < -FLAT_BAND:
        return -1
    return 0


def signal_hits(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = []
    for e in conn.execute(
        "SELECT id, ticker, market, decided_at, decision, realized_return, price_return, "
        "situation_vector "
        "FROM memory_entries WHERE outcome_at IS NOT NULL"
    ).fetchall():
        try:
            day = datetime.fromisoformat(e["decided_at"]).astimezone(_KST).date().isoformat()
        except ValueError:
            continue
        action = str((json.loads(e["decision"] or "{}") or {}).get("action") or "HOLD").upper()
        if e["price_return"] is not None:
            realized_sign = _price_sign(float(e["price_return"]), "BUY")
        else:
            realized_sign = _price_sign(float(e["realized_return"] or 0.0), action)
        vector = json.loads(e["situation_vector"] or "[]")
        for a in conn.execute(
            "SELECT role, signal FROM analyst_reports WHERE date=? AND market=? AND ticker=?",
            (day, e["market"], e["ticker"]),
        ).fetchall():
            rows.append(
                {
                    "decision_id": int(e["id"]),
                    "role": a["role"],
                    "signal": SIGNAL_SIGN.get(str(a["signal"] or ""), 0),
                    "realized_sign": realized_sign,
                    "vector": vector,
                }
            )
    return pd.DataFrame(rows, columns=["decision_id", "role", "signal", "realized_sign", "vector"])


def _rate(sub: pd.DataFrame) -> tuple[float | None, int]:
    sub = sub[sub["signal"] != 0]
    if sub.empty:
        return None, 0
    return float((sub["signal"] == sub["realized_sign"]).mean()), int(len(sub))


def hit_rates(df: pd.DataFrame, *, query_vector: list[float] | None, k: int = 20) -> dict:
    out: dict[str, dict] = {}
    if df.empty:
        return out
    if query_vector is not None:
        sims = np.array([vec.cosine(query_vector, v) for v in df["vector"]])
    for role, sub in df.groupby("role"):
        hr, n = _rate(sub)
        nb_hr, nb_n = None, 0
        if query_vector is not None:
            idx = sub.index.to_numpy()
            order = np.argsort(-sims[idx])[:k]
            nb_hr, nb_n = _rate(sub.iloc[order])
        bias = float(sub["signal"].mean() - sub["realized_sign"].mean())
        out[str(role)] = {
            "hit_rate": None if hr is None else round(hr, 4),
            "n": n,
            "neighbor_hit_rate": None if nb_hr is None else round(nb_hr, 4),
            "neighbor_n": nb_n,
            "bias": round(bias, 4),
        }
    return out


def build_calibration(
    conn: sqlite3.Connection, *, min_decisions: int, query_vector: list[float] | None, asof: str
) -> dict | None:
    n = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entries WHERE outcome_at IS NOT NULL"
        ).fetchone()["n"]
    )
    active = n >= min_decisions
    report = None
    if active:
        df = signal_hits(conn)
        roles = hit_rates(df, query_vector=query_vector)
        report = schemas.CalibrationReport(
            asof=asof, n_decisions=n, active=True, min_decisions=min_decisions, roles=roles
        ).model_dump()
    created = datetime.now(UTC).isoformat()
    # 같은 날(UTC) 행이 있으면 갱신: 종목마다 1행씩 쌓이지 않도록 하루 1행
    existing = conn.execute(
        "SELECT id FROM calibration_runs WHERE substr(created_at, 1, 10)=? "
        "ORDER BY id DESC LIMIT 1",
        (created[:10],),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE calibration_runs SET created_at=?, n_decisions=?, active=?, json=? WHERE id=?",
            (created, n, int(active), json.dumps(report or {}, ensure_ascii=False), existing["id"]),
        )
        conn.commit()
        return report
    conn.execute(
        "INSERT INTO calibration_runs(created_at, n_decisions, active, json) VALUES (?, ?, ?, ?)",
        (
            created,
            n,
            int(active),
            json.dumps(report or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return report
