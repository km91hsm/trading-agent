"""신호 적중률 보정 (스펙 §9.3).

- signal_hits: analyst_reports(신호) × memory_entries(결과) 조인. 신호 BUY/STRONG_BUY → +1,
  SELL/STRONG_SELL → −1, 그 외 0. 결과 부호 = sign(price_return, 방향 무관 원수익 — 신호는 가격
  방향 예측이다). 보고서는 아침 분석의 것이라 아침 라운드(round 0) 엔트리만 조인한다.
- decision_quality: 결정의 판단 품질은 direction_score(memory/outcomes) 기준, 재평가 라운드는
  REEVAL_WEIGHT 로 가중을 낮춘다(전체 검토 L3).
- hit_rates: 역할별 적중률, 질의 벡터 코사인 이웃 k 개 적중률, bias(평균 신호 − 평균 결과 부호).
- build_calibration: 결과 엔트리 수 < min_decisions 면 None (calibration_runs 에 비활성 기록).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from trader.agents import schemas
from trader.config import KST
from trader.memory import store
from trader.memory import vector as vec

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
    """만기 엔트리 × 그날 애널리스트 보고서를 JOIN 1회로 (엔트리마다 질의하던 N+1 제거).

    analyst_reports.date 는 KST 운영일, decided_at 은 UTC ISO 라 SQLite 의 '+9 hours' 로 맞춘다."""
    rows = []
    sign_cache: dict[int, tuple[int, list]] = {}
    for e in conn.execute(
        "SELECT m.id, m.decision, m.realized_return, m.price_return, m.situation_vector, "
        "a.role, a.signal "
        "FROM memory_entries m JOIN analyst_reports a "
        "ON a.market = m.market AND a.ticker = m.ticker "
        "AND a.date = date(m.decided_at, '+9 hours') "
        "WHERE m.outcome_at IS NOT NULL AND m.round = 0 ORDER BY m.id, a.role"
    ).fetchall():
        eid = int(e["id"])
        if eid not in sign_cache:
            action = str((json.loads(e["decision"] or "{}") or {}).get("action") or "HOLD").upper()
            if e["price_return"] is not None:
                realized_sign = _price_sign(float(e["price_return"]), "BUY")
            else:
                realized_sign = _price_sign(float(e["realized_return"] or 0.0), action)
            sign_cache[eid] = (realized_sign, json.loads(e["situation_vector"] or "[]"))
        realized_sign, vector = sign_cache[eid]
        rows.append(
            {
                "decision_id": eid,
                "role": e["role"],
                "signal": SIGNAL_SIGN.get(str(e["signal"] or ""), 0),
                "realized_sign": realized_sign,
                "vector": vector,
            }
        )
    return pd.DataFrame(rows, columns=["decision_id", "role", "signal", "realized_sign", "vector"])


def decision_quality(conn: sqlite3.Connection) -> dict | None:
    """{n, hit_rate, avg_direction_score, avg_avoided_loss} — 아침 1, 재평가는 결정당 합
    REEVAL_WEIGHT.

    미보유 HOLD 의 direction_score 0 은 맞고 틀림이 없어 적중률 분모에서 뺀다."""
    rows = conn.execute(
        "SELECT decision_id, round, direction_score, avoided_loss FROM memory_entries "
        "WHERE outcome_at IS NOT NULL AND direction_score IS NOT NULL"
    ).fetchall()
    if not rows:
        return None
    # 재평가 라운드는 결정 하나당 합쳐서 REEVAL_WEIGHT — 재평가가 잦은 결정이 집계를 지배하지 않게
    n_re: dict = {}
    for r in rows:
        if int(r["round"] or 0) > 0:
            n_re[r["decision_id"]] = n_re.get(r["decision_id"], 0) + 1
    w = np.array(
        [
            1.0 if int(r["round"] or 0) == 0 else store.REEVAL_WEIGHT / n_re[r["decision_id"]]
            for r in rows
        ]
    )
    ds = np.array([float(r["direction_score"]) for r in rows])
    scored = ds != 0
    hit = float((w[scored] * (ds[scored] > 0)).sum() / w[scored].sum()) if scored.any() else None
    av = [
        (wi, float(r["avoided_loss"]))
        for wi, r in zip(w, rows, strict=True)
        if r["avoided_loss"] is not None
    ]
    avg_av = sum(a * b for a, b in av) / sum(a for a, _ in av) if av else None
    return schemas.DecisionCalibration(
        n=len(rows),
        hit_rate=None if hit is None else round(hit, 4),
        avg_direction_score=round(float((w * ds).sum() / w.sum()), 6),
        avg_avoided_loss=None if avg_av is None else round(avg_av, 6),
    ).model_dump()


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
    conn: sqlite3.Connection,
    *,
    min_decisions: int,
    query_vector: list[float] | None,
    asof: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict | None:
    n = store.matured_count(conn)
    active = n >= min_decisions
    report = None
    if active:
        df = signal_hits(conn)
        roles = hit_rates(df, query_vector=query_vector)
        report = schemas.CalibrationReport(
            asof=asof,
            n_decisions=n,
            active=True,
            min_decisions=min_decisions,
            roles=roles,
            decisions=decision_quality(conn),
        ).model_dump()
    created_dt = now().astimezone(UTC)
    created = created_dt.isoformat()
    # 같은 KST 날짜 행이 있으면 갱신: 종목마다 1행씩 쌓이지 않도록 하루 1행
    kst_day = created_dt.astimezone(KST).date().isoformat()
    existing = conn.execute(
        "SELECT id, created_at FROM calibration_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if existing is not None:
        prev_day = datetime.fromisoformat(existing["created_at"]).astimezone(KST).date().isoformat()
        if prev_day != kst_day:
            existing = None
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
