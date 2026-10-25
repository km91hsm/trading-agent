import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from helpers_agents import ok_decision, ok_report, ok_turn
from helpers_sources import T0

from trader.agents.schemas import Decision
from trader.config import AccountConfig, Settings
from trader.orchestrator import run_store, tools
from trader.orchestrator.run_store import read_checkpoint, run_dir

D = date(2026, 10, 9)


@pytest.fixture
def ctx(project, conn):
    accounts = [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]
    return tools.ToolContext(settings=Settings(), accounts=accounts, conn=conn, now=lambda: T0)


def _inputs(project, ticker="005930", market="KR"):
    d = run_dir(D, market, ticker)
    run_store.write_checkpoint(
        d,
        "00_inputs",
        {
            "meta": {
                "ticker": ticker,
                "market": market,
                "name": "삼성전자",
                "asof": T0.isoformat(),
            },
            "technical": {"quote": {"data": {"price": "71500"}, "received_at": "", "error": None}},
            "fundamental": {},
            "news_macro": {},
            "sentiment": {},
            "errors": [],
        },
    )
    return d


def test_checkpoint_write_ok_and_list(ctx, project):
    _inputs(project)
    r = tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="10_analyst_technical",
        payload=ok_report(),
        agent="analyst-technical",
    )
    assert r["ok"] is True and r["path"].endswith("10_analyst_technical.json")
    saved = read_checkpoint(run_dir(D, "KR", "005930"), "10_analyst_technical")
    assert saved["schema_version"] == 1 and saved["data"]["signal"] == "BUY"
    assert saved["call"]["agent"] == "analyst-technical"
    r = tools.checkpoint_list(ctx, ticker="005930", market="KR", date=D)
    assert r == {"ok": True, "stages": ["00_inputs", "10_analyst_technical"]}


def test_checkpoint_write_rejects_invalid(ctx, project):
    _inputs(project)
    r = tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="10_analyst_technical",
        payload=ok_report(score=2.0),
        agent="analyst-technical",
    )
    assert r["ok"] is False and "score" in r["error"]
    assert read_checkpoint(run_dir(D, "KR", "005930"), "10_analyst_technical") is None
    r = tools.checkpoint_write(
        ctx, ticker="005930", market="KR", date=D, stage="99_unknown", payload={}, agent="x"
    )
    assert r["ok"] is False and "알 수 없는" in r["error"]


def test_checkpoint_write_rejects_decision_stage(ctx, project):
    """LLM 이 30_decision 을 직접 써서 submit-decision 전제조건을 우회할 수 없다 (리뷰 C1)."""
    _inputs(project)
    r = tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="30_decision",
        payload=ok_decision(),
        agent="trader",
    )
    assert r["ok"] is False and "submit-decision" in r["error"]
    assert read_checkpoint(run_dir(D, "KR", "005930"), "30_decision") is None
    r = tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="25_research_summary",
        payload={},
        agent="x",
    )
    assert r["ok"] is False


def test_checkpoint_write_rejects_name_payload_mismatch(ctx, project):
    _inputs(project)
    w = lambda stage, payload: tools.checkpoint_write(  # noqa: E731
        ctx, ticker="005930", market="KR", date=D, stage=stage, payload=payload, agent="a"
    )
    assert w("10_analyst_technical", ok_report(role="sentiment"))["ok"] is False
    assert w("10_analyst_technical", ok_report(ticker="000660"))["ok"] is False
    assert w("10_analyst_bogus", ok_report(role="technical"))["ok"] is False
    assert w("20_debate_r0_bull", ok_turn("bear"))["ok"] is False
    assert w("20_debate_r1_bull", ok_turn("bull", round=0))["ok"] is False
    assert w("20_debate_r2_bull", ok_turn("bull", round=2))["ok"] is False  # debate_rounds=1
    assert w("20_debate_r1_bull", ok_turn("bull", round=1))["ok"] is True
    assert list(run_dir(D, "KR", "005930").glob("10_analyst_*")) == []


def test_tools_reject_path_traversal(ctx, project):
    """ticker·market·stage 가 경로로 쓰이므로 state/ 밖·토큰 캐시를 가리킬 수 없다 (리뷰 C2)."""
    _inputs(project)
    (project / "state" / "kis_token_real.json").write_text(
        '{"access_token": "SECRET"}', encoding="utf-8"
    )
    r = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="../../../../kis_token_real"
    )
    assert r["ok"] is False and "SECRET" not in json.dumps(r)
    assert tools.checkpoint_list(ctx, ticker="../../..", market="KR", date=D)["ok"] is False
    assert tools.checkpoint_list(ctx, ticker="005930", market="../etc", date=D)["ok"] is False
    assert tools.checkpoint_list(ctx, ticker="005930", market="XX", date=D)["ok"] is False
    assert not (project / "state" / "etc").exists()
    assert tools.checkpoint_list(ctx, ticker="BRK.B", market="US", date=D)["ok"] is True


def test_checkpoint_write_unescapes_html_entities(ctx, project):
    """Claude Code 가 서브에이전트 출력을 이스케이프하므로 &lt; 등을 되돌려 저장한다 (리뷰 I7)."""
    _inputs(project)
    tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="10_analyst_technical",
        payload=ok_report(bullets=["종가 &lt; SMA20 &amp; MACD&gt;0"]),
        agent="a",
    )
    saved = read_checkpoint(run_dir(D, "KR", "005930"), "10_analyst_technical")
    assert saved["data"]["bullets"] == ["종가 < SMA20 & MACD>0"]


def test_status_does_not_create_placeholder_dir(ctx, project):
    _inputs(project)
    tools.status(ctx, market="KR", date=D)
    assert not (project / "state" / "runs" / "2026-10-09" / "KR" / "_").exists()
    r = tools.status(ctx, market="US", date=D)
    assert r["ok"] and r["tickers"] == {}


def test_checkpoint_read_section(ctx, project):
    _inputs(project)
    r = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section="technical"
    )
    assert r["ok"] and r["data"]["quote"]["data"]["price"] == "71500"
    r = tools.checkpoint_read(ctx, ticker="005930", market="KR", date=D, stage="30_decision")
    assert r["ok"] is False
    r = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section="trader_context"
    )
    assert (
        r["ok"]
        and r["data"]["costs_bp"] == 30
        and r["data"]["portfolio"]["paper-main"]["positions"] == []
    )
    assert r["data"]["risk_limits"]["max_position_pct"] == 0.25


def _big_inputs(project, ticker="005930", market="KR"):
    """실기 크기의 00_inputs: 일봉 260·분봉 120·호가 10단·뉴스 20+10·커뮤니티 50."""
    d = run_dir(D, market, ticker)
    candle = {
        "date": "2026-10-08",
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "1.5",
        "volume": 1000,
        "time": None,
    }
    news = [
        {
            "title": "제목 " * 10,
            "summary": "요약 " * 100,
            "source": "s",
            "published_at": "2026-10-08T00:00:00+00:00",
            "url": f"https://x/{i}",
            "received_at": "",
        }
        for i in range(20)
    ]
    posts = [
        {
            "title": "글 " * 20,
            "body": "본문 " * 200,
            "author": "a",
            "posted_at": "2026-10-08T00:00:00+00:00",
            "url": f"https://c/{i}",
            "received_at": "",
            "source": "clien",
        }
        for i in range(50)
    ]
    sec = lambda data: {"data": data, "received_at": "", "error": None}  # noqa: E731
    run_store.write_checkpoint(
        d,
        "00_inputs",
        {
            "meta": {
                "ticker": ticker,
                "market": market,
                "name": "삼성전자",
                "asof": T0.isoformat(),
            },
            "technical": {
                "quote": sec({"price": "71500"}),
                "daily": sec([candle] * 260),
                "minute": sec([{**candle, "time": "153000"}] * 120),
                "orderbook": sec({"asks": [["1", 1]] * 10, "bids": [["1", 1]] * 10}),
                "indicators": sec({"sma_5": 1.0}),
            },
            "fundamental": {"ratios": sec({"ROE": 0.1})},
            "news_macro": {
                "news": sec({"stock": news, "macro": news[:10], "sources": {"google_rss": "ok"}}),
                "macro": {"vix": sec({"value": 15.0, "date": "2026-10-08"})},
            },
            "sentiment": {
                "investor_flow": sec(
                    [
                        {
                            "date": "2026-10-08",
                            "foreign_net": 1,
                            "institution_net": 1,
                            "individual_net": 1,
                        }
                    ]
                    * 20
                ),
                "community": sec({"posts": posts, "sources": {"clien": "ok"}}),
            },
            "errors": [],
        },
    )


def test_checkpoint_read_section_is_compact(ctx, project):
    """서브에이전트에 넘길 절은 Claude Code Bash 출력 한도(약 30KB)보다 훨씬 작아야 한다."""
    _big_inputs(project)
    for section in ("technical", "fundamental", "news_macro", "sentiment"):
        r = tools.checkpoint_read(
            ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section=section
        )
        assert r["ok"], section
        assert len(json.dumps(r["data"], ensure_ascii=False)) < 12_000, section
    t = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section="technical"
    )["data"]
    assert len(t["daily"]["data"]) == 30 and len(t["minute"]["data"]) == 20
    assert len(t["orderbook"]["data"]["asks"]) == 5 and t["indicators"]["data"]["sma_5"] == 1.0
    assert t["daily"]["note"].startswith("최근 30")
    n = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section="news_macro"
    )["data"]
    assert len(n["news"]["data"]["stock"]) == 15 and len(n["news"]["data"]["macro"]) == 8
    assert all(len(i["summary"]) <= 120 and "url" not in i for i in n["news"]["data"]["stock"])
    s = tools.checkpoint_read(
        ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section="sentiment"
    )["data"]
    assert len(s["community"]["data"]["posts"]) == 20
    assert all(len(p["body"]) <= 200 for p in s["community"]["data"]["posts"])
    # 절 없이 전체를 읽으면 압축하지 않는다 (파일 내용 그대로)
    full = tools.checkpoint_read(ctx, ticker="005930", market="KR", date=D, stage="00_inputs")[
        "data"
    ]
    assert len(full["technical"]["daily"]["data"]) == 260


def _realistic_reports_and_turns(ctx, project):
    """실기 크기(리포트 ≈3.6KB, 턴 ≈2KB)로 체크포인트를 채운다."""
    _big_inputs(project)
    for role in ("technical", "fundamental", "news_macro", "sentiment"):
        payload = ok_report(  # 실기 리포트 ≈ 3.6KB
            role=role,
            summary="요약 " * 60,
            bullets=["근거 " * 20] * 6,
            key_risks=["위험 " * 15] * 3,
            counter_evidence=["반대 " * 15] * 2,
            data_gaps=["공백 " * 10] * 2,
            evidence=[{"claim": "주장 " * 15, "source_id": f"{role}.x"}] * 5,
        )
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=payload,
            agent="a",
        )
    for rnd in (0, 1):
        for side in ("bull", "bear"):
            payload = ok_turn(  # 실기 턴 ≈ 2KB
                side,
                round=rnd,
                arguments=["논거 " * 30] * 5,
                rebuttals=["반박 " * 30] * 5 if rnd else [],
                what_would_change_my_mind=["조건 " * 10] * 3,
            )
            tools.checkpoint_write(
                ctx,
                ticker="005930",
                market="KR",
                date=D,
                stage=f"20_debate_r{rnd}_{side}",
                payload=payload,
                agent="r",
            )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)


def test_bundle_researcher_round0_has_reports_no_opponent(ctx, project):
    """파이썬이 입력을 묶어 주면 오케스트레이터가 요약할 여지가 없다 (실기 3차 반영)."""
    _realistic_reports_and_turns(ctx, project)
    r = tools.checkpoint_bundle(
        ctx, ticker="005930", market="KR", date=D, kind="researcher", round=0, side="bull"
    )
    assert r["ok"]
    b = r["data"]
    assert b["round"] == 0 and b["side"] == "bull" and len(b["reports"]) == 4
    assert b["own_previous"] is None and b["opponent_previous"] is None
    assert b["reports"][0]["role"] in ("technical", "fundamental", "news_macro", "sentiment")
    assert len(json.dumps(b, ensure_ascii=False).encode("utf-8")) < 24_000


def test_bundle_researcher_round1_has_both_previous_turns(ctx, project):
    _realistic_reports_and_turns(ctx, project)
    b = tools.checkpoint_bundle(
        ctx, ticker="005930", market="KR", date=D, kind="researcher", round=1, side="bear"
    )["data"]
    assert b["own_previous"]["side"] == "bear" and b["own_previous"]["round"] == 0
    assert b["opponent_previous"]["side"] == "bull" and b["opponent_previous"]["round"] == 0
    assert len(b["reports"]) == 4
    assert len(json.dumps(b, ensure_ascii=False).encode("utf-8")) < 24_000


def test_bundle_trader_has_reports_summary_context_within_limit(ctx, project):
    _realistic_reports_and_turns(ctx, project)
    r = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert r["ok"]
    b = r["data"]
    assert len(b["reports"]) == 4 and b["research_summary"]["preferred"] in ("round0", "final")
    assert "round0" in b["research_summary"] and "final" in b["research_summary"]
    assert (
        b["context"]["costs_bp"] == 30
        and b["context"]["portfolio"]["paper-main"]["positions"] == []
    )
    assert len(json.dumps(b, ensure_ascii=False).encode("utf-8")) < 24_000
    assert b["context"]["market_view"] is None  # 오늘 시장 세션 제안 없음 (Phase 18)
    from trader.control import control

    control.set_flag(
        ctx.conn,
        "market_view:KR",
        {"date": D.isoformat(), "stance": "RISK_OFF", "hedge_weight": 0.1, "leveraged": False},
    )
    b2 = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")["data"]
    assert b2["context"]["market_view"]["stance"] == "RISK_OFF"
    assert "note" not in b  # 실기 크기에서는 손실 축약 없이 들어가야 한다
    assert "evidence" not in b["reports"][0]  # 트레이더 묶음은 evidence 를 뺀다
    assert set(b["research_summary"]["round0"]["bull"]) < set(
        b["research_summary"]["final"]["bull"]
    )


def test_bundle_requires_prerequisites(ctx, project):
    _inputs(project)
    assert (
        tools.checkpoint_bundle(
            ctx, ticker="005930", market="KR", date=D, kind="researcher", round=0, side="bull"
        )["ok"]
        is False
    )
    assert (
        tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")["ok"]
        is False
    )
    assert (
        tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="nope")["ok"]
        is False
    )


def test_submit_requires_reports_and_summary(ctx, project):
    _inputs(project)
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] is False and "전제조건" in r["error"]
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] is False and "25_research_summary" in r["error"]
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    assert tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)["ok"]
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] is True and len(r["executions"]) == 2
    assert read_checkpoint(run_dir(D, "KR", "005930"), "30_decision")["data"]["action"] == "BUY"
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert isinstance(rc["data"]["approved"], bool) and rc["data"]["dry_run"] is True
    assert read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main") is not None
    assert read_checkpoint(run_dir(D, "US", "005930"), "40_risk_check_x") is None
    # 결정·리포트·토론이 원장에 적재된다
    assert ctx.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM analyst_reports").fetchone()[0] == 2
    assert ctx.conn.execute("SELECT COUNT(*) FROM debate_turns").fetchone()[0] == 2


def test_submit_decision_runs_executor(ctx, project):
    """ctx.dry_run=False + 브로커가 있으면 submit-decision 이 모의 auto 계좌에 주문한다."""
    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash

    _inputs(project)
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    ctx.brokers = {a.id: FakeBroker(a, cash=cash) for a in ctx.accounts}
    ctx.dry_run = False
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] and r["executions"][0]["order"]["accepted"]
    assert len(ctx.brokers["paper-main"].orders) == 1  # 수량은 지정가 71000 기준 → 14주
    assert ctx.brokers["paper-main"].orders[0].qty == 14
    assert ctx.brokers["real-main"].orders == []
    # 같은 날 재제출은 기존 결정을 돌려주고 다시 실행하지 않는다 (리뷰 C1)
    r2 = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r2["ok"] and r2.get("already") is True
    assert len(ctx.brokers["paper-main"].orders) == 1


def test_submit_rejects_invalid_decision(ctx, project):
    _inputs(project)
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    r = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, payload=ok_decision(target_weight=0.9)
    )
    assert r["ok"] is False and read_checkpoint(run_dir(D, "KR", "005930"), "30_decision") is None


def test_summarize_requires_both_sides(ctx, project):
    _inputs(project)
    r = tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    assert r["ok"] is False


def test_status_lists_tickers_and_sessions(ctx, project):
    _inputs(project)
    _inputs(project, ticker="000660")
    r = tools.status(ctx, market="KR", date=D)
    assert r["ok"] and set(r["tickers"]) == {"005930", "000660"}
    assert r["tickers"]["005930"] == ["00_inputs"]
    assert r["sessions_today"] == 0 and r["cap"] == Settings().llm.daily_session_cap


def test_cli_tool_outputs_json_only(project, monkeypatch):
    from typer.testing import CliRunner

    from trader.cli import app

    _inputs(project)
    r = CliRunner().invoke(
        app,
        [
            "tool",
            "checkpoint",
            "list",
            "--ticker",
            "005930",
            "--market",
            "KR",
            "--date",
            "2026-10-09",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout) == {"ok": True, "stages": ["00_inputs"]}
    r = CliRunner().invoke(
        app,
        [
            "tool",
            "checkpoint",
            "write",
            "--ticker",
            "005930",
            "--market",
            "KR",
            "--date",
            "2026-10-09",
            "--stage",
            "10_analyst_technical",
            "--agent",
            "analyst-technical",
            "--json",
            json.dumps(ok_report()),
        ],
    )
    assert r.exit_code == 0 and json.loads(r.stdout)["ok"] is True


def test_cli_tool_error_is_json_exit1(project):
    from typer.testing import CliRunner

    from trader.cli import app

    r = CliRunner().invoke(
        app,
        [
            "tool",
            "submit-decision",
            "--ticker",
            "005930",
            "--market",
            "KR",
            "--date",
            "2026-10-09",
            "--json",
            json.dumps(ok_decision()),
        ],
    )
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and "전제조건" in out["error"]


# --- Phase 6 이연 소항목 ---------------------------------------------------------------


def test_trader_bundle_includes_portfolio_per_account(ctx, project):
    from decimal import Decimal

    from trader.ledger import repo

    _inputs(project)
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    repo.snapshot_positions(
        ctx.conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal("1000000"),
        cash=Decimal("500000"),
        positions=[
            {
                "ticker": "005930",
                "market": "KR",
                "qty": 5,
                "avg_price": "70000",
                "current_price": "71500",
                "eval_amount": "357500",
            }
        ],
    )
    r = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    pf = r["data"]["context"]["portfolio"]
    assert pf["paper-main"]["nav"] == "1000000" and pf["paper-main"]["positions"][0]["qty"] == 5
    assert pf["paper-main"]["mode"] == "auto" and pf["real-main"]["positions"] == []


def test_checkpoint_write_records_model(ctx, project):
    _inputs(project)
    r = tools.checkpoint_write(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="10_analyst_technical",
        payload=ok_report(),
        agent="analyst-technical",
        model="haiku",
    )
    assert r["ok"]
    saved = read_checkpoint(run_dir(D, "KR", "005930"), "10_analyst_technical")
    assert saved["call"]["model"] == "haiku"


def test_validate_ids_rejects_dots():
    import pytest

    for bad in (".", "..", "..."):
        with pytest.raises(tools.ToolInputError):
            tools._validate_ids(bad, "KR")
    tools._validate_ids("BRK.B", "US")  # 점이 섞인 정상 티커는 허용


# --- Phase 7: 05_quant 투입 ---------------------------------------------------------------


def _promote_quant(project):
    from datetime import UTC, datetime

    from helpers_ml import synthetic_features

    from trader.ml import dataset, features, registry
    from trader.ml.models import quant

    df = dataset.make_labels(
        synthetic_features(2, 120, signal=True), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    v = registry.save(
        "quant",
        quant.QuantModel(cols).fit(X, y),
        {"features": cols, "label": {"horizon_days": 5}},
        now=lambda: datetime(2026, 10, 17, tzinfo=UTC),
    )
    registry.promote("quant", v)
    feats = synthetic_features(2, 120, signal=True)
    feats["ticker"] = feats["ticker"].replace({"T00": "005930", "T01": "000660"})
    features.write_rows(feats)
    return v


def test_ensure_quant_skips_without_promoted_model(ctx, project):
    _inputs(project)
    r = tools.ensure_quant(ctx, ticker="005930", market="KR", date=D)
    assert r is None and read_checkpoint(run_dir(D, "KR", "005930"), "05_quant") is None


def test_ensure_quant_writes_report_and_bundle_context(ctx, project):
    _inputs(project)
    v = _promote_quant(project)
    r = tools.ensure_quant(ctx, ticker="005930", market="KR", date=D)
    assert r["model"] == f"quant@{v}" and r["role"] == "quant"
    cp = read_checkpoint(run_dir(D, "KR", "005930"), "05_quant")
    assert cp["data"]["ticker"] == "005930" and cp["call"]["agent"] == "quant"
    assert (
        tools.ensure_quant(ctx, ticker="005930", market="KR", date=D)["model"] == f"quant@{v}"
    )  # 멱등
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert b["data"]["context"]["quant"]["p_up"] == cp["data"]["p_up"]
    rd = tools.checkpoint_read(ctx, ticker="005930", market="KR", date=D, stage="05_quant")
    assert rd["ok"] and rd["data"]["data"]["role"] == "quant"


def test_ensure_quant_without_features_row_records_gap(ctx, project):
    _inputs(project)
    _promote_quant(project)
    r = tools.ensure_quant(ctx, ticker="999999", market="KR", date=D)
    assert r is None  # 피처 행이 없으면 05_quant 를 쓰지 않는다


def test_ensure_quant_disabled_or_broken_never_raises(ctx, project):
    import yaml

    _inputs(project)
    v = _promote_quant(project)
    (project / "config" / "ml.yaml").write_text(
        yaml.safe_dump({"models": {"quant": {"enabled": False}}}), encoding="utf-8"
    )
    assert tools.ensure_quant(ctx, ticker="005930", market="KR", date=D) is None
    (project / "config" / "ml.yaml").unlink()
    (project / "state" / "models" / "quant" / v / "model.joblib").write_bytes(b"garbage")
    assert tools.ensure_quant(ctx, ticker="005930", market="KR", date=D) is None


# --- Phase 8: 35_calibration ---------------------------------------------------------


def test_ensure_calibration_inactive_below_60(ctx, project):
    from helpers_memory import seed_matured

    _inputs(project)
    seed_matured(ctx.conn, 59)
    assert tools.ensure_calibration(ctx, ticker="005930", market="KR", date=D) is None
    assert read_checkpoint(run_dir(D, "KR", "005930"), "35_calibration") is None
    b = tools.trader_context(ctx, read_checkpoint(run_dir(D, "KR", "005930"), "00_inputs"), "KR")
    assert b["calibration"] is None


def test_ensure_calibration_active_at_60_and_in_context(ctx, project):
    from helpers_memory import seed_matured

    _inputs(project)
    seed_matured(ctx.conn, 60)
    r = tools.ensure_calibration(ctx, ticker="005930", market="KR", date=D)
    assert r["active"] and r["roles"]["technical"]["n"] == 60
    cp = read_checkpoint(run_dir(D, "KR", "005930"), "35_calibration")
    assert cp["data"]["asof"] and cp["call"]["agent"] == "calibration"
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert b["data"]["context"]["calibration"]["roles"]["technical"]["hit_rate"] is not None
    assert tools.ensure_calibration(ctx, ticker="005930", market="KR", date=D)["active"]  # 멱등


# --- Phase 8: 메모리 되먹임 ------------------------------------------------------------


def _ready_for_decision(ctx, project):
    _inputs(project)
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)


def test_trader_context_memory_empty_during_warmup(ctx, project):
    from helpers_memory import seed_matured

    _ready_for_decision(ctx, project)
    seed_matured(ctx.conn, 49)
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert (
        b["data"]["context"]["memory"] == []
        and b["data"]["context"]["memory_status"]["warmup"] is True
    )


def test_trader_context_memory_top5_after_warmup(ctx, project):
    from helpers_memory import seed_matured

    _ready_for_decision(ctx, project)
    seed_matured(ctx.conn, 50)
    from trader.memory import store

    store.set_reflection(
        ctx.conn, 1, {"decision_review": {"verdict": "GOOD", "note": "x"}, "lesson": "교훈"}
    )
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    mem = b["data"]["context"]["memory"]
    assert len(mem) == 5 and b["data"]["context"]["memory_status"]["warmup"] is False
    assert set(mem[0]) == {
        "id",
        "ticker",
        "decided_at",
        "situation_summary",
        "decision",
        "realized_return",
        "alpha_vs_benchmark",
        "lesson",
        "score",
    }
    assert any(m["lesson"] == "교훈" for m in mem)


def test_submit_decision_records_memory_entry_and_ids_used(ctx, project):
    from trader.memory import store

    _ready_for_decision(ctx, project)
    r = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, payload=ok_decision(memory_ids_used=["7", "x"])
    )
    assert r["ok"] and r["memory_entry_id"]
    e = store.get_entry(ctx.conn, r["memory_entry_id"])
    assert e["ticker"] == "005930" and e["decision"]["action"] == "BUY"
    assert e["decision"]["memory_ids_used"] == ["7", "x"] and len(e["situation_vector"]) == 12
    assert e["horizon_days"] == 10  # ok_decision 의 horizon_days
    r2 = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r2.get("already") is True
    assert ctx.conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 1


def test_ensure_calibration_respects_enabled_flag(ctx, project):
    from helpers_memory import seed_matured

    _inputs(project)
    seed_matured(ctx.conn, 60)
    (project / "config" / "ml.yaml").write_text(
        "models:\n  calibration: {enabled: false, min_decisions: 60}\n", encoding="utf-8"
    )
    assert tools.ensure_calibration(ctx, ticker="005930", market="KR", date=D) is None
    assert read_checkpoint(run_dir(D, "KR", "005930"), "35_calibration") is None


def test_memory_context_records_vix_change(ctx):
    from trader.memory import store

    def inputs(value, day):
        return {
            "meta": {"ticker": "005930"},
            "news_macro": {"macro": {"vix": {"data": {"value": value, "date": day}}}},
        }

    tools._memory_context(ctx, inputs(20.0, "2026-10-15"), "KR")
    tools._memory_context(ctx, inputs(27.0, "2026-10-16"), "KR")
    assert store.latest_vix_change(ctx.conn) == pytest.approx(0.35)
    tools._memory_context(ctx, {"meta": {"ticker": "005930"}}, "KR")  # macro 없음 → 유지
    assert store.latest_vix_change(ctx.conn) == pytest.approx(0.35)


def test_record_memory_decided_at_follows_run_date(ctx, project):
    # now 는 2026-10-09T01:00Z(KST 10-09) 이고 run date 가 10-09 → now 그대로;
    # run date 가 10-01 이면 그날 15:30 KST = 06:30Z
    from trader.ledger import repo

    dec = Decision(**ok_decision())
    for d in (date(2026, 10, 9), date(2026, 10, 1)):
        repo.ingest_checkpoint(
            ctx.conn, d, "KR", "005930", "30_decision", {"data": dec.model_dump()}
        )
        tools._record_memory(ctx, dec, "KR", d)
    rows = ctx.conn.execute("SELECT decided_at FROM memory_entries ORDER BY id").fetchall()
    assert [r["decided_at"] for r in rows] == [
        T0.isoformat(),
        "2026-10-01T06:30:00+00:00",
    ]


def test_market_dir_helper_creates_dir(project):
    from trader.orchestrator import run_store

    d = run_store.market_dir(date(2026, 10, 9), "KR", None, create=True)
    assert d.is_dir() and d.name == "KR" and d.parent.name == "2026-10-09"
    assert not run_store.market_dir(date(2026, 10, 10), "US", None).exists()


def test_ensure_calibration_query_vector_uses_run_date(ctx, project, monkeypatch):
    from helpers_memory import seed_matured

    from trader.calibration import model as cal
    from trader.memory import vector as vec
    from trader.ml import features as feat

    _inputs(project)
    seed_matured(ctx.conn, 60)
    rows = []
    for day, roc in ((date(2026, 10, 1), 1.0), (date(2026, 10, 20), 9.0)):
        rows.append(
            {
                "date": day,
                "ticker": "005930",
                "market": "KR",
                "close": 100.0,
                "volume": 1,
                "roc_10": roc,
            }
        )
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = float("nan")
    feat.write_rows(df[feat.FEATURE_COLUMNS])
    seen = {}
    real = cal.build_calibration

    def spy(conn, **kw):
        seen["qv"] = kw["query_vector"]
        return real(conn, **kw)

    monkeypatch.setattr(cal, "build_calibration", spy)
    tools.ensure_calibration(ctx, ticker="005930", market="KR", date=date(2026, 10, 5))
    assert seen["qv"][0] == vec.situation_vector({"roc_10": 1.0, "close": 100.0}, "KR")[0]


def _ready_for_submit(ctx, project):
    """보고서 2개·토론·요약·00_inputs 를 만들어 submit-decision 전제조건을 채운다."""
    _inputs(project)
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)


def test_submit_resumes_execution_when_decision_saved_but_not_executed(ctx, project):
    """30_decision 저장 뒤 execute 가 끊겼으면 재제출이 판정·주문·메모리를 마쳐야 한다.

    기존엔 already=true 로 기존 결정만 돌려주고 40_risk_check 도 risk_checks 행도 없이 끝났다."""
    _ready_for_submit(ctx, project)
    d = run_dir(D, "KR", "005930")
    dec = Decision(**ok_decision())
    run_store.write_checkpoint(d, "30_decision", {"schema_version": 1, "data": dec.model_dump()})
    assert read_checkpoint(d, "40_risk_check_paper-main") is None
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] and r["already"] is True and r["resumed_execution"] is True
    assert r["executions"] and r["executions"][0]["account_id"] == "paper-main"
    assert read_checkpoint(d, "40_risk_check_paper-main") is not None
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM risk_checks").fetchone()["n"] >= 1
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM memory_entries").fetchone()["n"] == 1
    # 판정이 있으면 재제출은 실행을 반복하지 않는다
    r2 = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r2["ok"] and r2["already"] is True and "resumed_execution" not in r2
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM risk_checks").fetchone()["n"] == (
        r["executions"].__len__()
    )


def test_submit_without_ledger_connection_does_not_leave_decision_file(ctx, project):
    """원장 연결이 없으면 30_decision 을 쓰기 전에 거부해야 재시도가 멱등 분기에 걸리지 않는다."""
    _ready_for_submit(ctx, project)
    ctx.conn = None
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] is False and "원장" in r["error"]
    assert read_checkpoint(run_dir(D, "KR", "005930"), "30_decision") is None


def test_submit_rejects_stop_loss_at_or_above_price_for_any_action(ctx, project):
    """손절선이 현재가 이상이면 action 과 무관하게 제출을 거부한다.

    게이트는 BUY/ADD 주문만 막았고 30_decision 은 저장돼 monitor 가 그 손절선으로 보유분을
    즉시 전량 매도했다 (HOLD/ADD 에 LLM 이 손절선을 현재가 위로 적는 실수 한 번 = 전량 청산)."""
    _ready_for_submit(ctx, project)
    for action in ("HOLD", "ADD"):
        r = tools.submit_decision(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            payload=ok_decision(action=action, stop_loss=72000.0, target_weight=0.1),
        )
        assert r["ok"] is False and "손절선" in r["error"], action
    assert read_checkpoint(run_dir(D, "KR", "005930"), "30_decision") is None
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0


def test_bundle_without_decision_has_no_previous_decision(ctx, project):
    _ready_for_submit(ctx, project)
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert b["ok"] and b["data"].get("previous_decision") is None


def test_section_read_for_fundamental_and_sentiment_includes_quote(ctx, project):
    """두 md 가 '함께 오는 quote 는 현재가' 라고 하므로 절 출력에 현재가를 실어 준다."""
    _inputs(project)
    for section in ("fundamental", "sentiment"):
        r = tools.checkpoint_read(
            ctx, ticker="005930", market="KR", date=D, stage="00_inputs", section=section
        )
        assert r["ok"] and r["data"]["quote"]["data"]["price"] == "71500", section


def test_dry_run_risk_checks_do_not_block_live_resume(ctx, project, monkeypatch):
    """아침 `--dry-run`(또는 FAKE) 이 남긴 40_ 판정은 실행이 아니다 — 실전 세션의 재제출은 실행을
    재개해야 한다(이전엔 '이미 실행' 으로 보고 그날 주문을 내지 않았다)."""
    from helpers_agents import ok_report, ok_turn

    _inputs(project)
    d = run_dir(D, "KR", "005930")
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent=f"analyst-{role}",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side, 0),
            agent=f"researcher-{side}",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    ctx.dry_run = True
    first = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, payload=ok_decision(action="HOLD")
    )
    assert first["ok"] and not first.get("already")
    assert read_checkpoint(d, "40_risk_check_paper-main")["data"]["dry_run"] is True
    ctx.dry_run = False
    second = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, payload=ok_decision(action="HOLD")
    )
    assert second["ok"] and second["already"] and second.get("resumed_execution") is True
    assert read_checkpoint(d, "40_risk_check_paper-main")["data"]["dry_run"] is False
    third = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, payload=ok_decision(action="HOLD")
    )
    assert third["already"] and not third.get("resumed_execution")
