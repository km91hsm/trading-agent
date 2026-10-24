"""시장 세션 도구(Phase 18): market bundle/write/summarize·submit-market-view."""

import pytest
from helpers_agents import ok_report, ok_turn
from helpers_sources import T0

from trader.agents import fake
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.orchestrator import tools
from trader.orchestrator.run_store import market_dir, read_checkpoint, write_checkpoint

D = T0.date()


@pytest.fixture
def ctx(project, conn):
    accounts = [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto")
    ]
    return tools.ToolContext(settings=Settings(), accounts=accounts, conn=conn, now=lambda: T0)


def _inputs(market="KR", state="bear"):
    d = market_dir(D, market, None, create=True)
    write_checkpoint(
        d,
        "_market_inputs",
        {
            "data": {
                "market": market,
                "date": D.isoformat(),
                "proxy": "KOSPI200",
                "regime": {"state": state, "count": 2},
                "hedge": {"held": []},
                "sector_weights": [{"sector": "반도체", "weight": 0.4}],
                "macro": {},
                "macro_news": [],
            }
        },
    )
    return d


def _proposal(**over):
    p = {
        "market": "KR",
        "stance": "RISK_OFF",
        "hedge_weight": 0.12,
        "leveraged": False,
        "confidence": 0.7,
        "horizon_days": 20,
        "rationale": "200일선 아래·변동성 확대",
        "invalidation_conditions": ["지수가 200일선을 회복"],
    }
    p.update(over)
    return p


def test_bundle_requires_inputs_then_report(ctx):
    assert tools.market_bundle(ctx, market="KR", date=D, kind="analyst")["ok"] is False
    _inputs()
    b = tools.market_bundle(ctx, market="KR", date=D, kind="analyst")
    assert b["ok"] and b["data"]["scope"] == "market" and b["data"]["regime"]["state"] == "bear"
    r = tools.market_bundle(ctx, market="KR", date=D, kind="researcher", round=0, side="bull")
    assert r["ok"] is False and "_market_analyst" in r["error"]
    assert tools.market_bundle(ctx, market="XX", date=D, kind="analyst")["ok"] is False


def test_write_validates_stage_round_and_side(ctx):
    _inputs()
    rep = ok_report(ticker="KOSPI200", role="news_macro")
    bad_stage = tools.market_write(ctx, market="KR", date=D, stage="30_decision", payload=rep)
    assert bad_stage["ok"] is False
    w = tools.market_write(
        ctx, market="KR", date=D, stage="_market_analyst", payload=rep, agent="analyst-market"
    )
    assert w["ok"] and read_checkpoint(market_dir(D, "KR"), "_market_analyst")["call"]["agent"] == (
        "analyst-market"
    )
    bad = tools.market_write(
        ctx, market="KR", date=D, stage="_market_debate_r0_bull", payload=ok_turn("bear", 0)
    )
    assert bad["ok"] is False and "불일치" in bad["error"]
    too_far = tools.market_write(
        ctx, market="KR", date=D, stage="_market_debate_r5_bull", payload=ok_turn("bull", 5)
    )
    assert too_far["ok"] is False and "debate_rounds" in too_far["error"]
    schema = tools.market_write(
        ctx, market="KR", date=D, stage="_market_analyst", payload={"ticker": "x"}
    )
    assert schema["ok"] is False and "스키마" in schema["error"]


def test_researcher_bundle_round1_needs_both_turns_and_summary_submit_flow(ctx, conn):
    _inputs()
    tools.market_write(
        ctx,
        market="KR",
        date=D,
        stage="_market_analyst",
        payload=ok_report(ticker="KOSPI200", role="news_macro", signal="SELL", score=-0.5),
    )
    assert tools.market_summarize(ctx, market="KR", date=D)["ok"] is False  # 턴 없음
    assert tools.submit_market_view(ctx, market="KR", date=D, payload=_proposal())["ok"] is False
    r0 = tools.market_bundle(ctx, market="KR", date=D, kind="researcher", round=0, side="bull")
    assert r0["ok"] and r0["data"]["opponent_previous"] is None and len(r0["data"]["reports"]) == 1
    for side in ("bull", "bear"):
        tools.market_write(
            ctx, market="KR", date=D, stage=f"_market_debate_r0_{side}", payload=ok_turn(side, 0)
        )
    r1 = tools.market_bundle(ctx, market="KR", date=D, kind="researcher", round=1, side="bear")
    assert r1["ok"] and r1["data"]["opponent_previous"]["side"] == "bull"
    assert tools.market_bundle(ctx, market="KR", date=D, kind="strategist")["ok"] is False
    s = tools.market_summarize(ctx, market="KR", date=D)
    assert s["ok"] and "sycophantic" in s
    st = tools.market_bundle(ctx, market="KR", date=D, kind="strategist")
    assert st["ok"] and st["data"]["hedge"]["max_weight_pct"] == 0.2
    assert st["data"]["sector_weights"][0]["sector"] == "반도체"
    bad_market = tools.submit_market_view(ctx, market="KR", date=D, payload=_proposal(market="US"))
    assert bad_market["ok"] is False
    out = tools.submit_market_view(ctx, market="KR", date=D, payload=_proposal())
    assert out["ok"] and out["hedge_weight"] == 0.12
    view = read_checkpoint(market_dir(D, "KR"), "_market_view")
    assert view["data"]["stance"] == "RISK_OFF" and view["call"]["agent"] == "strategist"
    flag = control.get_flag(conn, "market_view:KR")
    assert flag["date"] == D.isoformat() and flag["hedge_weight"] == 0.12
    assert tools.market_view_today(conn, "KR", D)["stance"] == "RISK_OFF"
    assert tools.market_view_today(conn, "KR", D.replace(day=D.day + 1)) is None  # 어제 것은 안 씀
    assert tools.market_view_today(conn, "US", D) is None


def test_fake_market_session_writes_all_stages(ctx, conn):
    _inputs(state="bear")
    written = fake.run_fake_market(ctx, market="KR", date=D)
    assert written[0] == "_market_analyst" and written[-1] == "_market_view"
    assert control.get_flag(conn, "market_view:KR")["stance"] == "RISK_OFF"
    _inputs("US", state="bull")
    fake.run_fake_market(ctx, market="US", date=D)
    v = control.get_flag(conn, "market_view:US")
    assert v["stance"] == "RISK_ON" and v["hedge_weight"] == 0.0
