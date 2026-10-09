import json
from datetime import date
from decimal import Decimal

import pytest
from helpers_agents import ok_decision, ok_report, ok_turn
from helpers_sources import T0

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
    assert r["ok"] and r["data"]["costs_bp"] == 30 and r["data"]["portfolio"] is None
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
    assert b["context"]["costs_bp"] == 30 and b["context"]["portfolio"] is None
    assert len(json.dumps(b, ensure_ascii=False).encode("utf-8")) < 24_000
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
    assert len(ctx.brokers["paper-main"].orders) == 1  # 가격은 00_inputs quote 71500 → 13주
    assert ctx.brokers["paper-main"].orders[0].qty == 13
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
