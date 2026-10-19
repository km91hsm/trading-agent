import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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
    # 미만기 엔트리를 가장 최근·최고 importance 로 두어 엠바고 필터가 없으면 반드시 1위가 되게 한다
    _entry(conn, 99, matured=False, decided_at=NOW, importance=1.0)
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET)
    assert r["warmup"] is False and r["breaker"] is None
    assert all(e["outcome_at"] for e in r["entries"]) and len(r["entries"]) == 5
    assert all(e["ticker"] != "T099" for e in r["entries"])
    everything = store.retrieve(conn, vector=VEC, now=NOW, settings=SET, k=100)["entries"]
    assert len(everything) == 50 and all(e["ticker"] != "T099" for e in everything)


def test_breaker_is_per_market(conn):
    for i in range(1, 51):
        _entry(conn, i)
    nav_kr = [("2026-10-01", Decimal(100)), ("2026-10-02", Decimal(88))]
    assert store.circuit_breaker(
        conn, nav_series=nav_kr, vix_change=None, settings=SET, market="KR"
    )
    nav_us = [("2026-10-01", Decimal(100)), ("2026-10-02", Decimal(101))]
    assert (
        store.circuit_breaker(conn, nav_series=nav_us, vix_change=None, settings=SET, market="US")
        is None
    )
    # US 판정이 KR 브레이커를 해제하지 않는다
    kr = store.retrieve(conn, vector=VEC, now=NOW, settings=SET, market="KR")
    assert kr["entries"] == [] and kr["breaker"]["active"]
    us = store.retrieve(conn, vector=VEC, now=NOW, settings=SET, market="US")
    assert us["breaker"] is None
    assert store.breaker_state(conn, "KR") and store.breaker_state(conn, "US") is None


def test_vix_change_recorded_and_trips_breaker(conn):
    assert store.record_vix(conn, value=20.0, date="2026-10-15") is None
    assert (
        store.record_vix(conn, value=20.0, date="2026-10-15") is None
    )  # 같은 날 재기록은 변화 없음
    assert store.record_vix(conn, value=27.0, date="2026-10-16") == pytest.approx(0.35)
    assert store.latest_vix_change(conn) == pytest.approx(0.35)
    st = store.circuit_breaker(
        conn, nav_series=[], vix_change=store.latest_vix_change(conn), settings=SET, market="KR"
    )
    assert st and "VIX" in st["reason"]


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


def test_record_decision_requires_decision_id(conn):
    with pytest.raises(ValueError):
        store.record_decision(
            conn,
            decision_id=None,
            ticker="T",
            market="KR",
            account_id="p",
            decided_at="2026-10-17T00:00:00+00:00",
            decision={},
            row=None,
            horizon_days=5,
        )


def test_retrieve_hydrates_only_top_k(conn):
    """점수 계산엔 벡터·시각·층·importance 만 필요하다. 전 행의 decision·reflection JSON 을 파싱하지
    않고 상위 k 만 복원해도 결과(순서·점수·필드)는 같다."""
    for i in range(1, 56):
        _entry(conn, i, vec=[0.5, -0.5] * 6, decided_at=NOW - timedelta(days=200))
    best = _entry(conn, 60, vec=VEC, decided_at=NOW - timedelta(days=8))
    stmts: list[str] = []
    conn.set_trace_callback(stmts.append)
    r = store.retrieve(conn, vector=VEC, now=NOW, settings=SET, k=3)
    conn.set_trace_callback(None)
    assert [e["id"] for e in r["entries"]][0] == best and len(r["entries"]) == 3
    assert all("decision" in e and "situation_vector" in e and "score" in e for e in r["entries"])
    full = [s for s in stmts if "SELECT *" in s]
    assert full and all("IN (" in s for s in full)  # 전 행 SELECT * 없음


def test_record_decision_updates_unmatured_entry_with_new_action(conn):
    """재평가(31_decision_r<n>)가 같은 decisions 행을 SELL 로 바꾸면 만기 전 메모리 엔트리의 결정도
    따라가야 한다. 첫 기록만 남으면 실제로 팔아 버린 종목을 BUY 로 채점한다."""
    a = _entry(conn, 1, matured=False)
    b = store.record_decision(
        conn,
        decision_id=1,
        ticker="T001",
        market="KR",
        account_id="paper-main",
        decided_at=NOW.isoformat(),
        decision={"action": "SELL", "target_weight": 0.0, "confidence": 0.9},
        row=None,
        horizon_days=3,
    )
    assert a == b
    e = store.get_entry(conn, a)
    assert e["decision"]["action"] == "SELL" and e["horizon_days"] == 3
    # 만기 결과가 채워진 엔트리는 바꾸지 않는다
    store.fill_outcome(conn, a, realized_return=0.01, alpha=0.0, outcome_at=NOW.isoformat())
    store.record_decision(
        conn,
        decision_id=1,
        ticker="T001",
        market="KR",
        account_id="paper-main",
        decided_at=NOW.isoformat(),
        decision={"action": "BUY"},
        row=None,
        horizon_days=5,
    )
    assert store.get_entry(conn, a)["decision"]["action"] == "SELL"
