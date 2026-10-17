from datetime import UTC, date, datetime, timedelta

import pytest
from helpers_agents import ok_decision, ok_report
from pydantic import ValidationError

from trader.agents import registry, schemas
from trader.config import AccountConfig, Settings
from trader.ledger import repo
from trader.memory import store
from trader.orchestrator import reflect, tools

NOW = datetime(2026, 10, 17, tzinfo=UTC)
D = date(2026, 10, 9)


def _accounts():
    return [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")]


def _matured(conn, i=1, ticker="005930"):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", ticker, "30_decision", {"data": ok_decision(ticker=ticker)}
    )
    repo.ingest_checkpoint(
        conn, D, "KR", ticker, "10_analyst_technical", {"data": ok_report(role="technical")}
    )
    eid = store.record_decision(
        conn,
        decision_id=dec_id,
        ticker=ticker,
        market="KR",
        account_id="paper-main",
        decided_at=(NOW - timedelta(days=8)).isoformat(),
        decision={"action": "BUY"},
        row=None,
        horizon_days=5,
    )
    store.fill_outcome(
        conn,
        eid,
        realized_return=0.03,
        alpha=0.01,
        outcome_at=(NOW - timedelta(days=1)).isoformat(),
    )
    return eid


def test_reflection_schema():
    ok = schemas.Reflection(
        entry_id=1,
        signal_review={"technical": {"verdict": "HIT", "note": "추세"}},
        decision_review={"verdict": "GOOD", "note": "x"},
        lesson=None,
        confidence=0.7,
    )
    assert ok.decision_review.verdict == "GOOD"
    with pytest.raises(ValidationError):
        schemas.Reflection(
            entry_id=1,
            signal_review={},
            decision_review={"verdict": "MAYBE", "note": ""},
            confidence=0.5,
        )
    from tests_paths import REPO_AGENTS

    a = registry.load_agent("reflector", REPO_AGENTS)
    assert a.model in ("haiku", "sonnet", "opus") and "lesson" in a.prompt and "한국어" in a.prompt


def test_pending_reflections_lists_matured_without_reflection(conn):
    eid = _matured(conn)
    items = reflect.pending(conn, market="KR")
    assert len(items) == 1 and items[0]["entry_id"] == eid
    assert (
        items[0]["analyst_signals"] == {"technical": "BUY"} and items[0]["realized_return"] == 0.03
    )
    store.set_reflection(conn, eid, {"decision_review": {"verdict": "GOOD"}})
    assert reflect.pending(conn, market="KR") == []


def test_fake_reflect_session_sets_reflections(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    eid = _matured(conn)
    res = reflect.run_reflect(
        Settings(), conn, _accounts(), market="KR", date=D, state=None, now=lambda: NOW
    )
    assert res.ok and res.fake and "_reflect" in res.stages_written
    e = store.get_entry(conn, eid)
    assert e["reflection"]["decision_review"]["verdict"] == "NEUTRAL" and e["tier"] == "short"
    assert (
        conn.execute("SELECT kind FROM runs ORDER BY id DESC LIMIT 1").fetchone()["kind"]
        == "reflect"
    )


def test_run_reflect_without_pending_skips_session(conn):
    res = reflect.run_reflect(
        Settings(), conn, _accounts(), market="KR", date=D, state=None, now=lambda: NOW
    )
    assert res.ok and res.stages_written == [] and res.session_id is None
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_submit_reflection_promotes_on_lesson(conn):
    eid = _matured(conn)
    ctx = tools.ToolContext(settings=Settings(), accounts=_accounts(), conn=conn, now=lambda: NOW)
    payload = [
        {
            "entry_id": eid,
            "signal_review": {"technical": {"verdict": "HIT", "note": ""}},
            "decision_review": {"verdict": "GOOD", "note": "좋음"},
            "lesson": "추세 추종 유효",
            "confidence": 0.8,
        }
    ]
    r = tools.submit_reflection(ctx, market="KR", date=D, payload=payload)
    assert r["ok"] and r["saved"] == 1
    assert store.get_entry(conn, eid)["tier"] == "mid"
    bad = tools.submit_reflection(
        ctx,
        market="KR",
        date=D,
        payload=[
            {
                "entry_id": 999,
                "signal_review": {},
                "decision_review": {"verdict": "GOOD", "note": ""},
                "confidence": 0.5,
            }
        ],
    )
    assert bad["ok"] is False
