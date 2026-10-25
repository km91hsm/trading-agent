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


def test_watch_targets_are_scoped_to_market(project, conn):
    r = screener.run_screen(
        Settings(),
        conn,
        _accounts(),
        market="US",
        date=D,
        candidates=[],
        cap_hit=True,
        now=lambda: NOW,
        watch=["005930", "AAPL", "KR:000660", "US:MSFT"],
    )
    assert [t["ticker"] for t in r["analyze_targets"]] == ["AAPL", "MSFT"]


def test_cap_fallback_picks_only_scored_candidates(conn):
    """세션 상한 폴백은 점수가 있는 후보만 고른다 (유니버스 확대 뒤 앞쪽 종목이 전부 '피처 없음'
    인데 그 셋을 '정량 상위' 로 고르던 결함)."""
    from trader.config import AccountConfig, Settings
    from trader.scan import screener

    cands = [
        {"ticker": "111111", "exchange": None, "score": None, "reasons": ["피처 없음"]},
        {"ticker": "222222", "exchange": None, "score": None, "reasons": ["피처 없음"]},
        {"ticker": "005930", "exchange": None, "score": 2.0, "reasons": ["모멘텀"]},
        {"ticker": "000660", "exchange": None, "score": 1.0, "reasons": ["모멘텀"]},
    ]
    accounts = [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")]
    r = screener.run_screen(
        Settings(), conn, accounts, market="KR", date=D, candidates=cands, cap_hit=True
    )
    assert [p["ticker"] for p in r["picks"]] == ["005930", "000660"]


def test_targets_skip_hedge_etf_and_take_exchange_from_position(project, conn):
    """보유 중 헤지 ETF 는 분석 대상이 아니고(국면 규칙이 판다), US 보유의 거래소는 잔고 응답·
    유니버스에서 찾는다 (리뷰 F24)."""
    from decimal import Decimal

    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash, Position
    from trader.config import Universe, UsSymbol

    def pos(ticker, market, exchange=None):
        return Position(
            ticker=ticker,
            market=market,
            qty=1,
            avg_price=Decimal(1),
            current_price=Decimal(1),
            eval_amount=Decimal(1),
            pnl_pct=0.0,
            exchange=exchange,
        )

    acct = AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto")
    cash = {
        "KR": Cash(currency="KRW", available=Decimal(1), total=Decimal(1)),
        "US": Cash(currency="USD", available=Decimal(1), total=Decimal(1)),
    }
    broker = FakeBroker(
        acct,
        cash=cash,
        positions=[
            pos("114800", "KR"),
            pos("005930", "KR"),
            pos("AAPL", "US", "NAS"),
            pos("SPY", "US"),
        ],
    )
    kr = screener.run_screen(
        Settings(),
        conn,
        [acct],
        market="KR",
        date=D,
        candidates=[],
        cap_hit=True,
        brokers={"paper-main": broker},
        now=lambda: NOW,
    )
    assert [t["ticker"] for t in kr["analyze_targets"]] == ["005930"]
    uni = Universe(kr=[], us=[UsSymbol(symbol="SPY", exchange="AMS")], watch=[], exclude=[])
    us = screener.run_screen(
        Settings(),
        conn,
        [acct],
        market="US",
        date=D,
        candidates=[],
        cap_hit=True,
        brokers={"paper-main": broker},
        now=lambda: NOW,
        universe=uni,
    )
    assert us["analyze_targets"] == [
        {"ticker": "AAPL", "exchange": "NAS"},
        {"ticker": "SPY", "exchange": "AMS"},
    ]


def test_screen_context_has_regime_and_todays_market_view(conn):
    """Phase 21: 스크리너 보조 입력 — 국면·오늘 시장 세션 제안. 없으면 None."""
    from trader.control import control
    from trader.scan.screener import screen_context

    assert screen_context(conn, "KR", D) == {"regime": None, "market_view": None}
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 2, "signals": {}})
    control.set_flag(
        conn,
        "market_view:KR",
        {
            "date": D.isoformat(),
            "stance": "RISK_OFF",
            "confidence": 0.8,
            "hedge_weight": 0.1,
            "rationale": "r",
            "invalidation_conditions": ["x"],
            "leveraged": False,
        },
    )
    ctx = screen_context(conn, "KR", D)
    assert ctx["regime"] == {"state": "bear", "count": 2}
    assert ctx["market_view"] == {
        "stance": "RISK_OFF",
        "confidence": 0.8,
        "hedge_weight": 0.1,
        "rationale": "r",
        "invalidation_conditions": ["x"],
    }
