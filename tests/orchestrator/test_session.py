import json
import os
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
        seen["agents"] = json.loads((Path(kw["cwd"]) / "agents.json").read_text(encoding="utf-8"))
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
    assert Path(seen["kw"]["cwd"]).name.startswith("trader-claude-")  # 임시 cwd (세션 뒤 삭제)
    stdin = json.loads(seen["kw"]["input"])
    assert stdin["tickers"][0]["ticker"] == "005930" and stdin["dry_run"] is True
    agents = seen["agents"]
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


def test_session_cwd_is_temp_and_removed(project, conn, accounts, monkeypatch):
    """세션 cwd 는 저장소 밖 임시 디렉터리이며 세션이 끝나면 지워진다 (상위 CLAUDE.md 탐색 차단)."""
    import subprocess as _sp

    _inputs()
    seen = {}

    def runner(cmd, **kw):
        seen["cwd"] = Path(kw["cwd"])
        seen["existed"] = seen["cwd"].is_dir() and (seen["cwd"] / "agents.json").is_file()
        return _sp.CompletedProcess(
            cmd,
            0,
            stdout='{"session_id": "s", "total_cost_usd": 0.1, "num_turns": 1, "result": "x"}',
            stderr="",
        )

    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
    )
    assert res.ok and seen["existed"]
    assert project not in seen["cwd"].parents and not seen["cwd"].exists()


def test_runner_oserror_is_recorded_not_left_running(project, conn, accounts, monkeypatch):
    """claude 실행파일이 사라진 경우 등 OSError 도 running 행을 error 로 닫고 결과로 돌려준다."""
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()

    def boom(cmd, **kw):
        raise FileNotFoundError("claude 없음")

    res = run_session(
        spec(),
        Settings(),
        conn,
        accounts,
        runner=boom,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok is False and "FileNotFoundError" in (res.error or "")
    assert conn.execute("SELECT status FROM agent_calls").fetchone()["status"] == "error"
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "error"


def test_prestage_failure_closes_run_and_removes_cwd(project, conn, accounts, monkeypatch):
    """find_claude·agents.json 생성 등 세션 기동 전 단계가 실패하면 runs 행이 running 으로 남고
    임시 cwd 가 누수됐다. 예외는 그대로 올리되 행을 error 로 닫고 디렉터리를 지운다."""
    import tempfile

    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    _inputs()
    before = set(Path(tempfile.gettempdir()).glob("trader-claude-*"))
    with pytest.raises(Exception):  # noqa: B017 - 종류(ConfigError 등)는 호출자가 구분한다
        run_session(
            spec(),
            Settings(),
            conn,
            accounts,
            claude_bin=Path("/x/claude"),
            agents_dir=project / "no-such-agents-dir",
            now=lambda: T0,
        )
    row = conn.execute("SELECT status, error FROM runs").fetchone()
    assert row["status"] == "error" and row["error"]
    assert set(Path(tempfile.gettempdir()).glob("trader-claude-*")) == before


def test_default_runner_kills_process_group_on_timeout(tmp_path):
    """세션이 타임아웃되면 claude 뿐 아니라 그 자식(tool 서브프로세스)도 죽어야 한다.
    subprocess.run 의 kill 은 직접 자식만 죽여 고아 프로세스가 주문을 계속 낼 수 있다."""
    import os
    import subprocess
    import sys
    import time

    import pytest

    from trader.orchestrator import session

    if sys.platform == "win32":
        pytest.skip("POSIX 프로세스 그룹 전용")
    marker = tmp_path / "grandchild.pid"
    child = (
        "import subprocess, sys, time, pathlib; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        session.run_claude([sys.executable, "-c", child], input="", timeout=2, cwd=str(tmp_path))
    pid = int(marker.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("손자 프로세스가 살아 있다 (프로세스 그룹 kill 안 됨)")


def test_session_records_num_turns(project, conn):
    """claude 결과의 num_turns 를 agent_calls 에 남긴다 (턴 수가 비용·실패 원인 분석의 단서)."""
    res = run_session(
        spec(),
        Settings(),
        conn,
        [],
        runner=lambda cmd, **kw: _ok_result(num_turns=7),
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert res.ok and res.num_turns == 7
    row = conn.execute("SELECT num_turns FROM agent_calls ORDER BY id DESC LIMIT 1").fetchone()
    assert row["num_turns"] == 7


def test_frozen_session_allows_trader_tool_and_rewrites_prompt(project, monkeypatch):
    """번들에서는 LLM 이 `trader tool …` 만 쓸 수 있어야 한다: --allowedTools 와 프롬프트의 명령
    문자열이 같지 않으면 도구 전제조건 게이트를 LLM 이 통과할 수 없다."""
    from trader.agents.registry import orchestrator_prompt
    from trader.platform import bundle

    monkeypatch.setattr(bundle, "tool_command", lambda: "trader tool")
    prompt = orchestrator_prompt(Settings(), REPO_AGENTS)
    assert "python -m trader tool" not in prompt and "trader tool status" in prompt
    cmd = build_command(
        spec(), Settings(), Path("/x/claude"), Path("/tmp/a.json"), session_id="s", prompt=prompt
    )
    pairs = {cmd[i]: cmd[i + 1] for i in range(2, len(cmd) - 1)}
    assert pairs["--allowedTools"] == "Bash(trader tool *)"


def test_session_env_path_starts_with_tool_bin_dir(project, conn, monkeypatch):
    from trader.platform import bundle

    monkeypatch.setattr(bundle, "tool_bin_dir", lambda: Path("/bundle/dir"))
    seen = {}

    def runner(cmd, **kw):
        seen["path"] = kw["env"]["PATH"]
        return _ok_result()

    run_session(
        spec(),
        Settings(),
        conn,
        [],
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: T0,
    )
    assert seen["path"].split(os.pathsep)[0] == "/bundle/dir"
