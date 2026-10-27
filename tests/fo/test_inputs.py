"""Phase 26 단계 3 Task 3.3: 파생 세션 입력(`fo/inputs.py`) — 절마다 asof·received_at, 체인 호출
≤18, 절 실패는 errors, 과거 asof 는 historical, 서브에이전트 묶음 형태."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from helpers_sources import T0

from fo._gate import contract, quote
from fo.test_executor import account, balance, settings
from trader.broker.fake import FakeFoBroker, FakeMarket
from trader.broker.models import Candle
from trader.control import control
from trader.fo import executor, inputs
from trader.orchestrator import tools
from trader.orchestrator.run_store import market_dir, read_checkpoint

D = T0.date()


def _candles(n=30):
    return [
        Candle(
            date=D - timedelta(days=n - i), open=Decimal(340 + i), high=Decimal(342 + i),
            low=Decimal(338 + i), close=Decimal(341 + i), volume=100 + i,
        )  # fmt: skip
        for i in range(n)
    ]


def _master_with_chain(strikes=range(330, 372, 2)):
    cs = [contract("105W11", "FUTURES")]
    for k in strikes:
        cs.append(contract(f"205W11{k}", "CALL", Decimal(k)))
        cs.append(contract(f"305W11{k}", "PUT", Decimal(k)))
    return cs


def _market(fail=None, chain_strikes=range(330, 372, 2)):
    fo_quotes = {"105W11": quote()}
    for k in chain_strikes:
        for kind, pre, dl in (("CALL", "205", 0.5), ("PUT", "305", -0.5)):
            fo_quotes[f"{pre}W11{k}"] = quote(
                code=f"{pre}W11{k}", kind=kind, price="5.00", delta=dl, iv=18.0,
                strike=Decimal(k), basis=None, volume=10 if kind == "CALL" else 12,
            )  # fmt: skip
    return FakeMarket(
        candles={"069500": _candles()}, fo_quotes=fo_quotes, fo_candles={"105W11": _candles()},
        kospi200=(Decimal("350"), T0), fail=fail, now=lambda: T0,
    )  # fmt: skip


def _ctx(conn, *, market=None, master=None, now=T0):
    acct = account()
    broker = FakeFoBroker(acct, balance=balance(), now=lambda: now)
    return executor.FoExecContext(
        settings=settings(), accounts=[acct], conn=conn, brokers={acct.id: broker},
        market_client=market or _market(), master=master or _master_with_chain(),
        now=lambda: now,
    )  # fmt: skip


def test_inputs_sections_have_asof_and_received_at(conn):
    ctx = _ctx(conn)
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    for sec in ("technical", "sentiment", "news_macro", "context"):
        assert inp[sec]["asof"] == T0.isoformat() and inp[sec]["received_at"], sec
    tech = inp["technical"]["data"]
    assert tech["meta"]["code"] == "105W11" and tech["quote"]["data"]["price"] == "350.5"
    assert tech["daily"]["data"][-1]["close"] == "370" and tech["indicators"]["data"]["sma_20"]
    assert inp["context"]["data"]["index"] == {"price": "350", "asof": T0.isoformat()}
    accts = inp["context"]["data"]["accounts"]
    assert accts[0]["id"] == "paper-fo" and accts[0]["nav"] == "100000000"
    assert inp["context"]["data"]["limits"]["max_leverage"] == 2.0
    assert inp["meta"]["historical"] is False and inp["meta"]["request_id"]
    assert inp["meta"]["proxy"] == tools.MARKET_PROXY_LABEL["KR"]
    # 거시 절의 sources.yaml 부재(테스트 프로젝트)만 남고 파생 절은 오류 없음
    assert all(k.startswith("news_macro.") for k in inp["errors"]), inp["errors"]
    p = inputs.save_inputs(inp, today=D, state=None)
    assert p.name == "_fo_inputs.json"
    saved = read_checkpoint(market_dir(D, "KR"), "_fo_inputs")
    assert saved["data"]["meta"]["date"] == D.isoformat()


def test_chain_calls_le_18(conn):
    """옵션 체인은 ATM ± 4 행사가(9개 × 콜·풋 = 18 시세)까지만 요청한다."""
    ctx = _ctx(conn)
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    chain = inp["context"]["data"]["option_chain"]
    assert 0 < len(chain) <= 18
    strikes = {c["strike"] for c in chain}
    assert len(strikes) <= 9 and "350" in strikes
    assert ctx.market_client.calls.count("get_option_chain") == 1


def test_section_failure_goes_to_errors(conn):
    ctx = _ctx(conn, market=_market(fail={"get_fo_daily", "get_kospi200"}))
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    tech = inp["technical"]["data"]
    assert tech["daily"]["error"] and tech["indicators"]["error"] == "no daily candles"
    assert tech["quote"]["error"] is None
    assert "technical.daily" in inp["errors"] and "context.index" in inp["errors"]
    assert inp["context"]["data"]["index"]["price"] is None
    # 지수 절이 비면 시세의 기초지수로 채우지 않는다(지수 신선도는 응답 시각 기준) — 체인은 그래도
    assert inp["context"]["data"]["option_chain"] == [] and "context.option_chain" in inp["errors"]


def test_historical_asof_flagged(conn):
    ctx = _ctx(conn)
    old = T0 - timedelta(seconds=settings().fo.max_input_age_s + 1)
    inp = inputs.build_inputs(ctx, today=D, asof=old, accounts=ctx.accounts)
    assert inp["meta"]["historical"] is True and inp["meta"]["asof"] == old.isoformat()
    assert inp["technical"]["data"]["daily"]["data"][-1]["date"] <= old.date().isoformat()


def test_bundle_analyst_market_shape(conn):
    """news_macro 묶음은 현물 시장 세션의 analyst 묶음(`_market_inputs` + scope·market·date)과
    같은 키 배치라 analyst-market md 가 그대로 읽는다."""
    from trader.orchestrator import market_view

    ctx = _ctx(conn)
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    b = inputs.bundle(inp, kind="analyst", role="news_macro")
    mv = market_view.build_inputs(conn, "KR", D, settings=settings(), asof=T0, now=lambda: T0)
    assert set(b) == {"scope", "market", "date", *mv}
    assert b["scope"] == "fo" and b["proxy"] == "KOSPI200" and b["market"] == "KR"


def test_bundle_technical_shape(conn):
    ctx = _ctx(conn)
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    b = inputs.bundle(inp, kind="analyst", role="technical")
    assert set(b["technical"]) == {"quote", "daily", "minute", "orderbook", "indicators"}
    assert b["ticker"] == "KOSPI200" and b["asof"] == T0.isoformat() and b["role"] == "technical"
    s = inputs.bundle(inp, kind="analyst", role="sentiment")
    assert "investor_flow" in s["sentiment"] and s["quote"]["data"]["price"] == "350.5"
    r = inputs.bundle(inp, kind="researcher", round=0, side="bull", reports=[{"role": "technical"}])
    assert r["scope"] == "fo" and r["reports"] == [{"role": "technical"}] and r["side"] == "bull"
    assert r["own_previous"] is None and r["regime"] == inp["context"]["data"]["regime"]
    t = inputs.bundle(
        inp, kind="trader", reports=[{"role": "technical"}], summary={"rounds": 0},
        position={"id": 1, "version": 2},
    )  # fmt: skip
    assert t["context"]["position"] == {"id": 1, "version": 2} and t["request_id"]
    assert t["context"]["accounts"][0]["id"] == "paper-fo"
    assert t["research_summary"] == {"rounds": 0}
    assert "option_chain" in t["context"] and "limits" in t["context"]


def test_context_has_regime_view_verified(conn):
    ctx = _ctx(conn)
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 3, "signals": {}})
    control.set_flag(
        conn, "market_view:KR", {"date": D.isoformat(), "stance": "RISK_OFF", "confidence": 0.8}
    )
    from trader.broker.fo import CONTRACT_VERSION

    key = executor.verified_key(settings(), account(), "FUTURES")
    control.set_flag(conn, key, CONTRACT_VERSION)
    inp = inputs.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    c = inp["context"]["data"]
    assert c["regime"]["state"] == "bear" and c["market_view"]["state"] == "RISK_OFF"
    assert c["limits"]["regime_cap"] == 2.0  # leverage_by_regime 미설정 → max_leverage
    assert c["verified"] == {
        "paper-fo": {"FUTURES": True, "OPTION_BUY": False, "OPTION_SELL": False}
    }
