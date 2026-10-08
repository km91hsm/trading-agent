import json
from datetime import date

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
    assert r["ok"] is True and r["risk"] == "phase4"
    assert read_checkpoint(run_dir(D, "KR", "005930"), "30_decision")["data"]["action"] == "BUY"
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert rc["data"]["approved"] is None and rc["data"]["reason"] == "phase4"
    assert read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main") is not None
    assert read_checkpoint(run_dir(D, "US", "005930"), "40_risk_check_x") is None


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
