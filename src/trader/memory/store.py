"""메모리 저장소 (스펙 §6.5): 3층 반감기 검색, 승격·삭제, 엠바고, 워밍업, 서킷브레이커.

검색 점수 = recency(2^(−age/half_life)) × 0.4 + relevance(코사인) × 0.4 + importance × 0.2,
재평가 라운드 엔트리는 × REEVAL_WEIGHT.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from trader.config import MemorySettings
from trader.control import control
from trader.ledger.db import now_iso
from trader.memory import vector as vec

TIERS = ("short", "mid", "long")
BIG_RETURN = 0.05
PRUNE_AGE_DAYS = 90
PRUNE_IMPORTANCE = 0.2
BREAKER_KEY = "memory_breaker"  # 시장별 키: memory_breaker:<market>
VIX_KEY = "vix_last"


def _breaker_key(market: str | None) -> str:
    return f"{BREAKER_KEY}:{market}" if market else BREAKER_KEY


W_RECENCY, W_RELEVANCE, W_IMPORTANCE = 0.4, 0.4, 0.2
REEVAL_WEIGHT = 0.5  # 재평가 라운드(round ≥ 1) 엔트리의 검색 점수 배율 — 아침 판단 우선


def _next_tier(tier: str) -> str:
    i = TIERS.index(tier) if tier in TIERS else 0
    return TIERS[min(i + 1, len(TIERS) - 1)]


def held_at(
    conn: sqlite3.Connection, *, account_id: str, market: str, ticker: str, d: str
) -> bool | None:
    """결정일 d 기준 보유 여부: d 이하 최신 스냅샷 날짜에 그 종목 qty>0 이면 True.
    그 계좌·시장 스냅샷이 아예 없으면 None(모름)."""
    last = conn.execute(
        "SELECT MAX(date) AS d FROM positions_snapshot WHERE account_id=? AND market=? AND date<=?",
        (account_id, market, d),
    ).fetchone()["d"]
    if last is None:
        return None
    row = conn.execute(
        "SELECT qty FROM positions_snapshot WHERE account_id=? AND date=? AND ticker=?",
        (account_id, last, ticker),
    ).fetchone()
    return bool(row and int(row["qty"] or 0) > 0)


def record_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    ticker: str,
    market: str,
    account_id: str,
    decided_at: str,
    decision: dict,
    row: dict | None,
    horizon_days: int,
    vector: list[float] | None = None,
    round_no: int = 0,
    held_at_decision: bool | None = None,
) -> int:
    """결정·라운드 1건당 엔트리 1건 ((decision_id, round) UNIQUE, 멱등). importance 초기값 =
    confidence.

    아침 결정(30_decision)은 round 0, 장중 재평가(31_decision_r<n>)는 round n 의 **별도 엔트리**다
    (전체 검토 L2 — 덮어쓰면 아침 판단의 채점이 사라진다). 같은 라운드로 다시 오면(세션 재시도)
    기존 id 를 돌려준다. held_at_decision 은 결정 시점 보유(판정 때 잔고 스냅샷)이고 HOLD
    채점에 쓴다."""
    if decision_id is None:
        raise ValueError("memory_entries 는 decision_id 가 있어야 멱등이 성립합니다")
    existing = conn.execute(
        "SELECT id FROM memory_entries WHERE decision_id=? AND round=?", (decision_id, round_no)
    ).fetchone()
    if existing:
        return int(existing["id"])
    v = vector or vec.situation_vector(row or {}, market)
    importance = float(decision.get("confidence") or 0.5)
    cur = conn.execute(
        "INSERT INTO memory_entries(ticker, market, account_id, decision_id, round, decided_at, "
        "situation_vector, situation_summary, decision, horizon_days, tier, importance, "
        "created_at, held_at_decision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'short', ?, ?, ?)",
        (
            ticker,
            market,
            account_id,
            decision_id,
            int(round_no),
            decided_at,
            json.dumps(v),
            vec.summary(row or {}, market),
            json.dumps(decision, ensure_ascii=False, default=str),
            horizon_days,
            max(0.0, min(1.0, importance)),
            now_iso(),
            None if held_at_decision is None else int(bool(held_at_decision)),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM memory_entries WHERE id=?", (entry_id,)).fetchone()
    return _hydrate(row) if row else None


def _hydrate(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["situation_vector"] = json.loads(d["situation_vector"] or "[]")
    d["decision"] = json.loads(d["decision"] or "{}")
    d["reflection"] = json.loads(d["reflection"]) if d.get("reflection") else None
    return d


def fill_outcome(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    realized_return: float,
    alpha: float,
    outcome_at: str,
    price_return: float | None = None,
    direction_score: float | None = None,
    avoided_loss: float | None = None,
    realized_pnl_pct: float | None = None,
) -> None:
    """결과 채움. realized_return·alpha 는 하위 호환(옛 채점), 판단 품질은 direction_score·
    avoided_loss(outcomes 모듈 docstring). 방향 점수(없으면 실현수익)나 회피 손실이 5% 이상이면
    승격."""
    e = get_entry(conn, entry_id)
    if e is None:
        return
    tier = e["tier"]
    importance = float(e["importance"] or 0.0)
    magnitude = abs(realized_return if direction_score is None else direction_score)
    if max(magnitude, avoided_loss or 0.0) >= BIG_RETURN:
        tier = _next_tier(tier)
        importance = min(1.0, importance + 0.3)
    conn.execute(
        "UPDATE memory_entries SET realized_return=?, alpha_vs_benchmark=?, outcome_at=?, tier=?, "
        "importance=?, price_return=?, direction_score=?, avoided_loss=?, realized_pnl_pct=? "
        "WHERE id=?",
        (
            float(realized_return),
            float(alpha),
            outcome_at,
            tier,
            importance,
            _opt(price_return),
            _opt(direction_score),
            _opt(avoided_loss),
            _opt(realized_pnl_pct),
            entry_id,
        ),
    )
    conn.commit()


def _opt(x: float | None) -> float | None:
    return None if x is None else float(x)


def set_reflection(conn: sqlite3.Connection, entry_id: int, reflection: dict) -> None:
    """반성 저장. lesson 이 있으면 한 층 승격."""
    e = get_entry(conn, entry_id)
    if e is None:
        return
    tier = e["tier"]
    importance = float(e["importance"] or 0.0)
    if reflection.get("lesson"):
        tier = _next_tier(tier)
        importance = min(1.0, importance + 0.2)
    conn.execute(
        "UPDATE memory_entries SET reflection=?, tier=?, importance=? WHERE id=?",
        (json.dumps(reflection, ensure_ascii=False, default=str), tier, importance, entry_id),
    )
    conn.commit()


def matured_count(conn: sqlite3.Connection) -> int:
    """만기 결정 수 — 아침 라운드(round 0)만 센다(재평가 엔트리로 워밍업·보정 기준이 부풀지
    않게)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entries WHERE outcome_at IS NOT NULL AND round=0"
        ).fetchone()["n"]
    )


def breaker_state(conn: sqlite3.Connection, market: str | None = None) -> dict | None:
    """시장별 브레이커(시장 없는 전역 플래그는 모든 시장에 적용). None 이면 켜진 것 아무거나."""

    def active(key: str) -> dict | None:
        b = control.get_flag(conn, key)
        return b if isinstance(b, dict) and b.get("active") else None

    if market:
        return active(_breaker_key(market)) or active(BREAKER_KEY)
    keys = [BREAKER_KEY] + [
        str(r["key"])
        for r in conn.execute(
            "SELECT key FROM control WHERE key LIKE ?", (f"{BREAKER_KEY}:%",)
        ).fetchall()
    ]
    return next((b for k in keys if (b := active(k))), None)


def record_vix(conn: sqlite3.Connection, *, value: float, date: str) -> float | None:
    """VIX 관측(날짜·값)을 기록하고 직전 다른 날짜 대비 변화율을 돌려준다 (없으면 None)."""
    last = control.get_flag(conn, VIX_KEY) or {}
    if last.get("date") == date:
        prev_date, prev_value = last.get("prev_date"), last.get("prev_value")
    else:
        prev_date, prev_value = last.get("date"), last.get("value")
    change = None
    if prev_value:
        change = float(value) / float(prev_value) - 1
    control.set_flag(
        conn,
        VIX_KEY,
        {
            "date": date,
            "value": float(value),
            "prev_date": prev_date,
            "prev_value": prev_value,
            "change": change,
        },
    )
    return change


def latest_vix_change(conn: sqlite3.Connection) -> float | None:
    last = control.get_flag(conn, VIX_KEY) or {}
    return last.get("change")


def retrieve(
    conn: sqlite3.Connection,
    *,
    vector: list[float],
    now: datetime,
    settings: MemorySettings,
    market: str | None = None,
    k: int | None = None,
) -> dict:
    """{entries, warmup, breaker}. 만기 전(outcome 없음) 제외, 워밍업·브레이커면 빈 목록."""
    k = k or settings.top_k
    breaker = breaker_state(conn, market)
    if breaker:
        return {"entries": [], "warmup": False, "breaker": breaker}
    if matured_count(conn) < settings.warmup:
        return {"entries": [], "warmup": True, "breaker": None}
    # 점수에 필요한 열만 읽어 순위를 매기고, 상위 k 만 전체 행(decision·reflection JSON)으로 복원
    sql = (
        "SELECT id, round, decided_at, tier, importance, situation_vector FROM memory_entries "
        "WHERE outcome_at IS NOT NULL"
    )
    args: list = []
    if market:
        sql += " AND market=?"
        args.append(market)
    half = {
        "short": settings.half_lives.short,
        "mid": settings.half_lives.mid,
        "long": settings.half_lives.long,
    }
    scores: list[tuple[float, int]] = []
    for row in conn.execute(sql, args).fetchall():
        try:
            age_days = (now - datetime.fromisoformat(row["decided_at"]).astimezone(UTC)).days
        except ValueError:
            age_days = 0
        hl = half.get(row["tier"], settings.half_lives.short)
        recency = 2 ** (-max(age_days, 0) / hl)
        relevance = max(0.0, vec.cosine(vector, json.loads(row["situation_vector"] or "[]")))
        score = (
            W_RECENCY * recency
            + W_RELEVANCE * relevance
            + W_IMPORTANCE * float(row["importance"] or 0)
        )
        if int(row["round"] or 0) > 0:
            score *= REEVAL_WEIGHT
        scores.append((round(score, 4), int(row["id"])))
    scores.sort(key=lambda t: (-t[0], t[1]))
    top = scores[:k]
    if not top:
        return {"entries": [], "warmup": False, "breaker": None}
    by_id = {
        int(r["id"]): _hydrate(r)
        for r in conn.execute(
            f"SELECT * FROM memory_entries WHERE id IN ({','.join('?' * len(top))})",
            [i for _, i in top],
        ).fetchall()
    }
    entries = []
    for score, i in top:
        e = by_id[i]
        e["score"] = score
        entries.append(e)
    return {"entries": entries, "warmup": False, "breaker": None}


def promote_and_prune(conn: sqlite3.Connection, *, now: datetime, settings: MemorySettings) -> dict:
    """short 층에서 90일 넘고 importance < 0.2 면 삭제. (승격은 결과·반성 시점에 이미 처리)"""
    deleted = 0
    for row in conn.execute(
        "SELECT id, decided_at, tier, importance FROM memory_entries"
    ).fetchall():
        try:
            age = (now - datetime.fromisoformat(row["decided_at"]).astimezone(UTC)).days
        except ValueError:
            continue
        if (
            row["tier"] == "short"
            and age > PRUNE_AGE_DAYS
            and float(row["importance"] or 0) < PRUNE_IMPORTANCE
        ):
            conn.execute("DELETE FROM memory_entries WHERE id=?", (row["id"],))
            deleted += 1
    conn.commit()
    return {"promoted": 0, "deleted": deleted}


def circuit_breaker(
    conn: sqlite3.Connection,
    *,
    nav_series: list[tuple[str, Decimal]],
    vix_change: float | None,
    settings: MemorySettings,
    market: str | None = None,
) -> dict | None:
    """드로다운 ≤ drawdown_stop_pct 또는 VIX 급등이면 그 시장의 플래그를 켜고 사유를 돌려준다."""
    reason = None
    if nav_series:
        peak = max(Decimal(str(v)) for _, v in nav_series)
        last = Decimal(str(nav_series[-1][1]))
        if peak > 0:
            dd = float(last / peak - 1)
            if dd <= settings.drawdown_stop_pct:
                reason = f"드로다운 {dd:.1%} ≤ {settings.drawdown_stop_pct:.0%}"
    if reason is None and vix_change is not None and vix_change >= settings.vix_spike_pct:
        reason = f"VIX 급등 {vix_change:+.0%} ≥ {settings.vix_spike_pct:.0%}"
    if reason:
        state = {"active": True, "reason": reason, "at": now_iso()}
        control.set_flag(conn, _breaker_key(market), state)
        return state
    control.set_flag(conn, _breaker_key(market), {"active": False, "reason": None, "at": now_iso()})
    return None
