import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from trader.agents import registry, schemas
from trader.config import AccountConfig, Settings
from trader.scan import screener

D = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
CANDS = [
    {"ticker": f"0000{i:02d}", "exchange": None, "score": 10 - i, "reasons": ["r"]}
    for i in range(8)
]


def _accounts():
    return [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")]


def test_screened_schema_limits():
    ok = schemas.Screened(picks=[{"ticker": "005930", "reason": "x"}], rejected=[])
    assert ok.picks[0].ticker == "005930"
    with pytest.raises(ValidationError):
        schemas.Screened(picks=[{"ticker": f"{i}", "reason": "x"} for i in range(6)], rejected=[])
    with pytest.raises(ValidationError):
        schemas.Screened(picks=[], rejected=[])


def test_screener_agent_loads_with_common_rules():
    from tests_paths import REPO_AGENTS

    a = registry.load_agent("screener", REPO_AGENTS)
    assert a.model == "haiku" and "picks" in a.prompt and "한국어" in a.prompt


def test_fake_screen_session_writes_screen_json(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = screener.run_screen(
        Settings(),
        conn,
        _accounts(),
        market="KR",
        date=D,
        candidates=CANDS,
        cap_hit=False,
        now=lambda: NOW,
    )
    assert [p["ticker"] for p in r["picks"]] == ["000000", "000001", "000002", "000003", "000004"]
    f = project / "state" / "runs" / "2026-10-09" / "KR" / "_screen.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert len(data["data"]["picks"]) == 5 and data["call"]["agent"] == "screener"
    assert r["analyze_targets"][0] == {"ticker": "000000", "exchange": None}
    assert r["session"]["fake"] is True


def test_cap_hit_reduces_picks_without_session(project, conn):
    r = screener.run_screen(
        Settings(),
        conn,
        _accounts(),
        market="KR",
        date=D,
        candidates=CANDS,
        cap_hit=True,
        now=lambda: NOW,
    )
    assert [p["ticker"] for p in r["picks"]] == ["000000", "000001", "000002"] and r[
        "session"
    ] is None
    assert conn.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0] == 0


def test_targets_include_positions_and_dedupe(project, conn, monkeypatch):
    from decimal import Decimal

    from trader.broker.fake import FakeBroker
    from trader.broker.models import Position

    monkeypatch.setenv("FAKE_CLAUDE", "1")
    pos = Position(
        ticker="000007",
        market="KR",
        qty=1,
        avg_price=Decimal(1),
        current_price=Decimal(1),
        eval_amount=Decimal(1),
        pnl_pct=0.0,
    )
    pos2 = Position(
        ticker="000001",
        market="KR",
        qty=1,
        avg_price=Decimal(1),
        current_price=Decimal(1),
        eval_amount=Decimal(1),
        pnl_pct=0.0,
    )
    brokers = {"paper-main": FakeBroker(_accounts()[0], positions=[pos, pos2])}
    r = screener.run_screen(
        Settings(),
        conn,
        _accounts(),
        market="KR",
        date=D,
        candidates=CANDS,
        cap_hit=False,
        now=lambda: NOW,
        brokers=brokers,
    )
    ids = [t["ticker"] for t in r["analyze_targets"]]
    assert ids.count("000001") == 1 and "000007" in ids and len(ids) == 6
