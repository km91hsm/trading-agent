import json
from datetime import date
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from helpers_sources import T0

from trader.agents.registry import AGENTS
from trader.config import AccountConfig, Settings
from trader.orchestrator import run_store, session
from trader.orchestrator.run_store import list_checkpoints, read_checkpoint, run_dir
from trader.orchestrator.session import SessionSpec, build_command, run_session

D = date(2026, 10, 9)
REPO_AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"


def _inputs(ticker="005930", market="KR"):
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


def spec(**over):
    base = {
        "market": "KR",
        "date": D,
        "tickers": [{"ticker": "005930", "exchange": None}],
        "dry_run": True,
    }
    base.update(over)
    return SessionSpec(**base)


@pytest.fixture
def accounts():
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto")
    ]


def test_build_command_flags(project):
    s = Settings(models={"orchestrator": "sonnet"})
    cmd = build_command(
        spec(), s, Path("/x/claude"), Path("/tmp/agents.json"), session_id="u-1", prompt="PROMPT"
    )
    assert cmd[:2] == ["/x/claude", "-p"]
    assert (
        "--bare" not in cmd
        and "--max-turns" not in cmd
        and "--append-system-prompt-file" not in cmd
    )
    pairs = {cmd[i]: cmd[i + 1] for i in range(2, len(cmd) - 1)}
    assert pairs["--output-format"] == "json" and pairs["--model"] == "sonnet"
    assert pairs["--tools"] == "Bash,Agent"
    assert pairs["--allowedTools"] == "Bash(python -m trader tool *)"
    assert pairs["--permission-mode"] == "dontAsk" and pairs["--setting-sources"] == ""
    assert pairs["--agents"] == "/tmp/agents.json" and pairs["--session-id"] == "u-1"
    assert pairs["--append-system-prompt"] == "PROMPT"
    assert float(pairs["--max-budget-usd"]) == s.llm.budget_usd
    assert "--disable-slash-commands" in cmd


def _ok_result(**over):
    r = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"done": true}',
        "session_id": "sess-1",
        "total_cost_usd": 0.42,
        "duration_ms": 1234,
        "num_turns": 9,
    }
    r.update(over)
    return CompletedProcess([], 0, stdout=json.dumps(r), stderr="")


def test_run_session_records_call_and_writes_agents_file(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")  # venv bin 이 PATH 에 없는 상황을 흉내 낸다
    _inputs()
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return _ok_result()

    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok and res.cost_usd == 0.42 and res.session_id == "sess-1"
    assert seen["kw"]["shell"] is False and seen["kw"]["encoding"] == "utf-8"
    assert seen["kw"]["env"]["CLAUDE_CODE_RETRY_WATCHDOG"] == "1"
    assert seen["kw"]["env"]["TRADER_HOME"] == str(project)
    # `python -m trader tool …` 이 venv 의 python 으로 풀리도록 PATH 앞에 실행파일 디렉터리를 둔다
    import os
    import sys

    assert seen["kw"]["env"]["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert Path(seen["kw"]["cwd"]).name == "claude_cwd" and Path(seen["kw"]["cwd"]).is_dir()
    stdin = json.loads(seen["kw"]["input"])
    assert stdin["tickers"][0]["ticker"] == "005930" and stdin["dry_run"] is True
    agents_file = Path(seen["cmd"][seen["cmd"].index("--agents") + 1])
    agents = json.loads(agents_file.read_text(encoding="utf-8"))
    assert set(agents) == set(AGENTS) and agents["trader"]["tools"] == []
    row = conn.execute("SELECT agent, status, cost_usd, session_id FROM agent_calls").fetchone()
    assert (row["agent"], row["status"], row["cost_usd"], row["session_id"]) == (
        "orchestrator",
        "ok",
        0.42,
        "sess-1",
    )
    run = conn.execute("SELECT kind, status FROM runs").fetchone()
    assert (run["kind"], run["status"]) == ("analyze", "ok")


def test_session_error_recorded_and_resumable(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    d = _inputs()
    run_store.write_checkpoint(
        d, "10_analyst_technical", {"schema_version": 1, "data": {}, "call": None}
    )
    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=lambda cmd, **kw: _ok_result(is_error=True, result="budget exceeded"),
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok is False and "budget" in res.error
    assert conn.execute("SELECT status FROM agent_calls").fetchone()["status"] == "error"
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "error"
    assert list_checkpoints(d) == ["00_inputs", "10_analyst_technical"]  # 보존


def test_session_non_json_output_is_error(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()
    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=lambda cmd, **kw: CompletedProcess([], 0, stdout="<html>", stderr=""),
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok is False and "JSON" in res.error


def test_daily_cap_blocks(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()
    s = Settings()
    s.llm.daily_session_cap = 1
    run_session(
        spec(),
        s,
        conn,
        accounts,
        runner=lambda cmd, **kw: _ok_result(),
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    with pytest.raises(session.PipelineError, match="상한"):
        run_session(
            spec(),
            s,
            conn,
            accounts,
            runner=lambda cmd, **kw: _ok_result(),
            claude_bin=Path("/x/claude"),
            agents_dir=REPO_AGENTS,
            now=lambda: T0,
        )


def test_daily_cap_ignores_fake_sessions(project, conn, accounts, monkeypatch):
    """개발 중 FAKE 를 아무리 돌려도 실제 세션 상한·비용 집계에 들어가지 않는다 (리뷰 I2)."""
    _inputs()
    s = Settings()
    s.llm.daily_session_cap = 1
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    run_session(spec(), s, conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0)
    run_session(spec(), s, conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0)
    monkeypatch.delenv("FAKE_CLAUDE")
    res = run_session(
        spec(),
        s,
        conn,
        accounts,
        runner=lambda cmd, **kw: _ok_result(),
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok
    from trader import status as status_mod

    text = status_mod.build_status(conn, s, accounts, project / "docs" / "TODO.md", now=T0)
    assert "- 1건, $0.42" in text


def test_running_row_recorded_before_session_and_duration_measured(
    project, conn, accounts, monkeypatch
):
    """시작 시 running 행을 먼저 쓰고 종료 시 파이썬 측 소요시간으로 갱신한다 (리뷰 I4)."""
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()
    from datetime import timedelta

    clock = [T0]

    def now():
        return clock[0]

    def runner(cmd, **kw):
        row = conn.execute("SELECT status FROM agent_calls").fetchone()
        assert row["status"] == "running"
        clock[0] = T0 + timedelta(seconds=250)
        return _ok_result(duration_ms=6838)

    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=now,
    )
    row = conn.execute("SELECT status, duration_ms FROM agent_calls").fetchone()
    assert row["status"] == "ok" and row["duration_ms"] == 250_000 and res.duration_ms == 250_000


def test_stdin_carries_asof(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()
    seen = {}

    def runner(cmd, **kw):
        seen.update(kw)
        return _ok_result()

    run_session(
        spec(asof=T0),
        Settings(),
        conn,
        accounts,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert json.loads(seen["input"])["asof"] == T0.isoformat()


def test_anonymize_true_is_rejected_until_wired(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    _inputs()
    from trader.config import ConfigError

    with pytest.raises(ConfigError, match="anonymize"):
        run_session(
            spec(), Settings(anonymize=True), conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0
        )


def test_fake_session_writes_all_stages(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    d = _inputs()
    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess 호출 금지")),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok and res.fake
    stages = list_checkpoints(d)
    for s in (
        "10_analyst_technical",
        "10_analyst_fundamental",
        "10_analyst_news_macro",
        "10_analyst_sentiment",
        "20_debate_r0_bull",
        "20_debate_r0_bear",
        "20_debate_r1_bull",
        "20_debate_r1_bear",
        "25_research_summary",
        "30_decision",
        "40_risk_check_paper-main",
    ):
        assert s in stages, s
    dec = read_checkpoint(d, "30_decision")
    assert dec["data"]["action"] == "HOLD" and dec["call"]["model"] == "fake"
    assert conn.execute("SELECT status FROM agent_calls").fetchone()["status"] == "ok"


def test_fake_skips_existing(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    d = _inputs()
    run_session(spec(), Settings(), conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0)
    first = (d / "10_analyst_technical.json").stat().st_mtime_ns
    (d / "20_debate_r1_bear.json").unlink()
    (d / "30_decision.json").unlink()
    res = run_session(spec(), Settings(), conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0)
    assert res.ok and res.stages_written == [
        "20_debate_r1_bear",
        "25_research_summary",
        "30_decision",
    ]
    assert (d / "10_analyst_technical.json").stat().st_mtime_ns == first


def test_fake_session_without_inputs_fails(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    res = run_session(spec(), Settings(), conn, accounts, agents_dir=REPO_AGENTS, now=lambda: T0)
    assert res.ok is False and "00_inputs" in res.error
