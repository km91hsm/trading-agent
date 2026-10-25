"""작업 대기열(jobs.py): kind 검증·중복 거부·FIFO claim·진행/종료·stale 정리."""

from datetime import UTC, datetime, timedelta

import pytest

from trader import jobs
from trader.ledger import db

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


def test_validate_rejects_unknown_kind_and_extra_params():
    with pytest.raises(ValueError, match="알 수 없는 작업"):
        jobs.validate("nope", {})
    with pytest.raises(ValueError):
        jobs.validate("features_backfill", {"years": 9})
    with pytest.raises(ValueError):
        jobs.validate("ml_train", {"bogus": 1})
    with pytest.raises(ValueError):
        jobs.validate("scan_analyze", {})  # market 필수
    assert jobs.validate("features_backfill", {}) == {"market": None, "years": 3}
    # US 종목 분석의 거래소는 선택(데몬이 유니버스로 보강)
    p = jobs.validate("analyze_ticker", {"market": "US", "ticker": "aapl"})
    assert p["ticker"] == "AAPL" and p["exchange"] is None and p["dry_run"] is True


def test_enqueue_dedupes_queued_and_running(conn):
    a = jobs.enqueue(conn, "ml_train", {"market": "KR"}, source="web", now=NOW)
    with pytest.raises(jobs.DuplicateRequest):
        jobs.enqueue(conn, "ml_train", {"market": "KR"}, source="web", now=NOW)
    # 다른 파라미터는 별개
    jobs.enqueue(conn, "ml_train", {"market": "US"}, source="web", now=NOW)
    row = jobs.claim_next(conn, now=NOW)
    assert row["id"] == a and row["status"] == "running"
    with pytest.raises(jobs.DuplicateRequest):  # running 도 중복
        jobs.enqueue(conn, "ml_train", {"market": "KR"}, source="web", now=NOW)
    jobs.finish(conn, a, "ok", result={"n": 1}, now=NOW)
    assert jobs.enqueue(conn, "ml_train", {"market": "KR"}, source="web", now=NOW) > a


def test_claim_next_is_fifo_and_conditional(conn):
    first = jobs.enqueue(conn, "features_derive", {}, source="web", now=NOW)
    second = jobs.enqueue(conn, "fills_sync", {}, source="web", now=NOW)
    # 다른 프로세스가 first 를 먼저 가져갔다
    conn.execute("UPDATE job_requests SET status='running' WHERE id=?", (first,))
    conn.commit()
    row = jobs.claim_next(conn, now=NOW)
    assert row["id"] == second
    assert jobs.claim_next(conn, now=NOW) is None


def test_finish_and_progress_roundtrip(conn):
    rid = jobs.enqueue(conn, "features_backfill", {"years": 2}, source="daemon", now=NOW)
    jobs.claim_next(conn, now=NOW)
    jobs.set_progress(conn, rid, "KR 005930: 10행")
    rows = jobs.list_recent(conn)
    assert rows[0]["progress"] == "KR 005930: 10행"
    assert rows[0]["params"] == {"market": None, "years": 2}
    jobs.finish(conn, rid, "error", error="boom", now=NOW)
    r = jobs.list_recent(conn)[0]
    assert r["status"] == "error" and r["error"] == "boom" and r["finished_at"]
    assert jobs.active_count(conn) == 0


def test_cancel_only_queued(conn):
    rid = jobs.enqueue(conn, "reflect", {}, source="web", now=NOW)
    assert jobs.cancel(conn, rid) is True
    assert jobs.list_recent(conn)[0]["status"] == "skipped"
    rid2 = jobs.enqueue(conn, "reflect", {}, source="web", now=NOW)
    jobs.claim_next(conn, now=NOW)
    assert jobs.cancel(conn, rid2) is False


def test_mark_stale_running_and_expire_queued(conn):
    old = NOW - timedelta(hours=7)
    a = jobs.enqueue(conn, "ml_train", {}, source="web", now=old)
    jobs.claim_next(conn, now=old)
    b = jobs.enqueue(conn, "rl_train", {}, source="web", now=old)
    c = jobs.enqueue(conn, "reflect", {}, source="web", now=NOW - timedelta(hours=1))
    assert jobs.mark_stale_running(conn, now=NOW) == 1
    assert jobs.expire_queued(conn, older_than=NOW - timedelta(hours=6)) == 1
    by = {r["id"]: r for r in jobs.list_recent(conn)}
    assert by[a]["status"] == "error" and "재시작" in by[a]["error"]
    assert by[b]["status"] == "skipped" and by[c]["status"] == "queued"


def test_list_recent_orders_active_first(conn):
    done = jobs.enqueue(conn, "ml_train", {}, source="web", now=NOW)
    jobs.claim_next(conn, now=NOW)
    jobs.finish(conn, done, "ok", now=NOW)
    q = jobs.enqueue(conn, "rl_train", {}, source="web", now=NOW)
    ids = [r["id"] for r in jobs.list_recent(conn)]
    assert ids == [q, done]


def test_job_id_for_maps_scheduled_kinds():
    assert jobs.job_id_for("scan_analyze", {"market": "KR"}) == "kr_scan_analyze"
    assert jobs.job_id_for("ml_train", {}) == "ml_train"
    assert jobs.job_id_for("features_backfill", {"years": 3}) == "features_backfill"
    assert jobs.is_heavy("fills_sync") is False and jobs.is_heavy("ml_train") is True


def test_label_is_korean(conn):
    assert jobs.label("scan_analyze", {"market": "KR"}) == "KR 스캔 → 분석"
    p = {"market": "KR", "ticker": "005930", "dry_run": True}
    assert "005930" in jobs.label("analyze_ticker", p)
    assert jobs.label("features_backfill", {"years": 3}) == "피처 백필 3년"


def test_schema_has_table(conn):
    assert db.current_version(conn) >= 11


def test_claim_next_prefers_priority_then_fifo(conn):
    """Phase 21: 재평가(priority 10)는 먼저 넣은 백필보다 먼저 꺼내진다. 같은 우선순위는 FIFO."""
    bf = jobs.enqueue(conn, "features_backfill", {"market": "KR"}, source="web", now=NOW)
    tr = jobs.enqueue(conn, "ml_train", {}, source="web", now=NOW)
    re = jobs.enqueue(
        conn,
        "reevaluate",
        {"market": "KR", "ticker": "005930", "trigger_id": 1},
        source="daemon",
        now=NOW,
    )
    assert jobs.PRIORITY["reevaluate"] == 10
    ids = [r["id"] for r in jobs.list_recent(conn)]
    assert ids == [re, bf, tr]  # 실행될 순서
    assert jobs.claim_next(conn, now=NOW)["id"] == re
    assert jobs.claim_next(conn, now=NOW)["id"] == bf
    # 명시 우선순위
    hi = jobs.enqueue(conn, "fills_sync", {}, source="cli", now=NOW, priority=5)
    assert jobs.claim_next(conn, now=NOW)["id"] == hi
    assert jobs.claim_next(conn, now=NOW)["id"] == tr
