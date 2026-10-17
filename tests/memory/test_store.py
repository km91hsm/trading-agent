import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.config import MemorySettings, Settings
from trader.control import control
from trader.ledger import db
from trader.memory import store

NOW = datetime(2026, 10, 17, tzinfo=UTC)
SET = MemorySettings()
VEC = [0.5] * 12


def _entry(conn, i, *, decided_at=None, vec=None, matured=True, realized=0.01, importance=None):
    eid = store.record_decision(
        conn,
        decision_id=i,
        ticker=f"T{i:03d}",
        market="KR",
        account_id="paper-main",
        decided_at=(decided_at or NOW - timedelta(days=10)).isoformat(),
        decision={"action": "BUY", "target_weight": 0.1, "confidence": 0.6},
        row={"roc_10": 1.0},
        horizon_days=5,
        vector=vec or VEC,
    )
    if matured:
        store.fill_outcome(
            conn,
            eid,
            realized_return=realized,
            alpha=realized - 0.002,
            outcome_at=(NOW - timedelta(days=1)).isoformat(),
        )
    if importance is not None:
        conn.execute("UPDATE memory_entries SET importance=? WHERE id=?", (importance, eid))
        conn.commit()
    return eid


def test_v5_tables_exist(conn):
    assert db.SCHEMA_VERSION >= 5
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memory_entries", "calibration_runs"} <= names


def test_settings_memory_defaults():
    s = Settings()
    assert s.memory.half_lives.short == 14 and s.memory.half_lives.long == 365
    assert s.memory.warmup == 50 and s.memory.drawdown_stop_pct == -0.10 and s.memory.top_k == 5


def test_record_decision_idempotent(conn):
    a = _entry(conn, 1, matured=False)
    b = store.record_decision(
        conn,
        decision_id=1,
        ticker="T001",
        market="KR",
        account_id="paper-main",
        decided_at=NOW.isoformat(),
        decision={"action": "SELL"},
        row=None,
        horizon_days=5,
    )
    assert a == b and conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 1
    row = conn.execute("SELECT tier, importance, situation_vector FROM memory_entries").fetchone()
    assert (
        row["tier"] == "short"
        and 0 <= row["importance"] <= 1
        and len(json.loads(row["situation_vector"])) == 12
    )


def test_retrieve_excludes_unmatured(conn):
    for i in range(1, 51):
        _entry(conn, i)
    _entry(conn, 99, matured=False)
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET)
    assert r["warmup"] is False and r["breaker"] is None
    assert all(e["outcome_at"] for e in r["entries"]) and len(r["entries"]) == 5
    assert all(e["ticker"] != "T099" for e in r["entries"])


def test_warmup_blocks_retrieval(conn):
    for i in range(1, 50):
        _entry(conn, i)
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET)
    assert r["warmup"] is True and r["entries"] == []
    _entry(conn, 50)
    assert store.retrieve(conn, vector=VEC, now=NOW, settings=SET)["warmup"] is False


def test_retrieve_scores_recency_relevance_importance(conn):
    for i in range(1, 51):
        _entry(conn, i, vec=[0.5, -0.5] * 6, decided_at=NOW - timedelta(days=200))  # 다른 방향
    recent_same = _entry(conn, 60, vec=VEC, decided_at=NOW - timedelta(days=8))
    old_same = _entry(conn, 61, vec=VEC, decided_at=NOW - timedelta(days=300))
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET, k=2)
    ids = [e["id"] for e in r["entries"]]
    assert ids[0] == recent_same and old_same in ids
    assert r["entries"][0]["score"] > r["entries"][1]["score"]
    assert store.retrieve(conn, vector=VEC, now=NOW, settings=SET, market="US")["entries"] == []


def test_promote_on_big_return_and_lesson(conn):
    big = _entry(conn, 1, realized=0.08)
    small = _entry(conn, 2, realized=0.01)
    assert (
        store.get_entry(conn, big)["tier"] == "mid"
        and store.get_entry(conn, small)["tier"] == "short"
    )
    store.set_reflection(
        conn, small, {"decision_review": {"verdict": "BAD"}, "lesson": "과열 구간 매수 금지"}
    )
    assert store.get_entry(conn, small)["tier"] == "mid"
    store.set_reflection(conn, big, {"lesson": "추세 추종 유효"})
    assert store.get_entry(conn, big)["tier"] == "long"  # mid → long


def test_prune_old_low_importance_short(conn):
    keep = _entry(conn, 1, decided_at=NOW - timedelta(days=100), importance=0.5)
    _entry(conn, 2, decided_at=NOW - timedelta(days=100), importance=0.1)  # 삭제 대상
    young = _entry(conn, 3, decided_at=NOW - timedelta(days=10), importance=0.1)
    r = store.promote_and_prune(conn, now=NOW, settings=SET)
    assert r["deleted"] == 1
    ids = {row["id"] for row in conn.execute("SELECT id FROM memory_entries")}
    assert ids == {keep, young}


def test_circuit_breaker_on_drawdown(conn):
    for i in range(1, 51):
        _entry(conn, i)
    navs = [
        ((NOW - timedelta(days=30 - d)).date().isoformat(), Decimal(100 - (d if d > 20 else 0)))
        for d in range(31)
    ]  # 고점 100 → 90 (−10%)
    b = store.circuit_breaker(conn, nav_series=navs, vix_change=None, settings=SET)
    assert b and "드로다운" in b["reason"] and control.get_flag(conn, "memory_breaker")["active"]
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET)
    assert r["entries"] == [] and r["breaker"]["active"]
    assert (
        store.circuit_breaker(
            conn,
            nav_series=[("2026-10-01", Decimal(100)), ("2026-10-02", Decimal(99))],
            vix_change=None,
            settings=SET,
        )
        is None
    )
    assert store.retrieve(conn, vector=VEC, now=NOW, settings=SET)["breaker"] is None
    assert store.circuit_breaker(conn, nav_series=[], vix_change=0.35, settings=SET)[
        "reason"
    ].startswith("VIX")
