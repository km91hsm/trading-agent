"""Phase 25 단계 5: 단발 LLM 판정 quick.ask_json (claude 없이 러너 주입)."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from trader.agents.schemas import ScalpVerdict
from trader.config import Settings
from trader.orchestrator import quick, tools

REPO_AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
GOOD = {
    "go": True,
    "size": "half",
    "take_profit_pct": 0.012,
    "stop_pct": 0.006,
    "max_hold_min": 20,
    "reason": "돌파 확인",
    "confidence": 0.7,
}


@pytest.fixture(autouse=True)
def _no_fake(monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)


def _runner(result_text, *, rc=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.update(cmd=cmd, kw=kw)
        lines = [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.4}}},
            },
            {
                "type": "result",
                "is_error": False,
                "result": result_text,
                "total_cost_usd": 0.01,
                "session_id": "s",
            },
        ]
        return CompletedProcess(cmd, rc, stdout="\n".join(json.dumps(x) for x in lines), stderr="")

    return run


def _ask(conn, runner, cap=60, **kw):
    return quick.ask_json(
        Settings(),
        conn,
        agent="scalp-judge",
        payload={"ticker": "005930"},
        schema=ScalpVerdict,
        cap=cap,
        timeout_s=5,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=REPO_AGENTS,
        now=lambda: NOW,
        **kw,
    )


def test_ask_json_parses_valid(conn):
    seen = {}
    v = _ask(conn, _runner("```json\n" + json.dumps(GOOD) + "\n```", seen=seen))
    assert v.go is True and v.size == "half"
    pairs = {seen["cmd"][i]: seen["cmd"][i + 1] for i in range(1, len(seen["cmd"]) - 1)}
    assert pairs["--tools"] == "" and pairs["--output-format"] == "stream-json"
    assert "--agents" not in seen["cmd"] and json.loads(seen["kw"]["input"]) == {"ticker": "005930"}
    row = conn.execute("SELECT agent, status, cost_usd FROM agent_calls").fetchone()
    assert tuple(row) == ("scalp_judge", "ok", 0.01)
    assert conn.execute("SELECT five_hour_util FROM llm_rate_limits").fetchone()[0] == 0.4


def test_ask_json_timeout_returns_none(conn):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    assert _ask(conn, boom) is None
    assert conn.execute("SELECT status FROM agent_calls").fetchone()[0] == "error"


def test_ask_json_passes_cancel_and_handles_cancelled(conn):
    import threading

    from trader.orchestrator import session

    ev = threading.Event()
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        raise session.SessionCancelled(cmd, 5)

    assert _ask(conn, run, cancel=ev) is None
    assert seen["cancel"] is ev
    row = conn.execute("SELECT status, error FROM agent_calls").fetchone()
    assert row[0] == "error" and "종료" in row[1]


def test_ask_json_schema_violation_or_text_is_none(conn):
    assert _ask(conn, _runner("모르겠다")) is None
    assert _ask(conn, _runner(json.dumps({**GOOD, "stop_pct": 0.5}))) is None


def test_ask_json_cap_blocks(conn):
    called = []

    def run(cmd, **kw):
        called.append(1)
        return _runner(json.dumps(GOOD))(cmd, **kw)

    assert _ask(conn, run, cap=1) is not None
    assert _ask(conn, run, cap=1) is None and len(called) == 1


def test_ask_json_not_counted_in_session_cap(conn):
    _ask(conn, _runner(json.dumps(GOOD)))
    assert tools.sessions_today(conn, NOW) == 0


def test_fake_mode_is_conservative(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    assert _ask(conn, None).go is False


def test_probe_usage_records_observation_outside_session_cap(conn, monkeypatch):
    """사용량 확인: haiku 단발 호출의 rate_limit_event 를 source='probe' 로 남기고, 분석 세션 수에
    들어가지 않는다. FAKE 면 호출 없음."""
    seen: dict = {}
    out = quick.probe_usage(
        Settings(),
        conn,
        runner=_runner("ok", seen=seen),
        claude_bin=Path("claude"),
        now=lambda: NOW,
    )
    assert out == {"ok": True}
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "haiku"
    row = conn.execute("SELECT source, five_hour_util FROM llm_rate_limits").fetchone()
    assert row["source"] == "probe" and abs(row["five_hour_util"] - 0.4) < 1e-9
    assert conn.execute("SELECT agent, status FROM agent_calls").fetchone()[:] == (
        "usage_probe",
        "ok",
    )
    assert tools.sessions_today(conn, NOW) == 0
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    assert quick.probe_usage(Settings(), conn, runner=_runner("ok")) == {"skipped": "FAKE"}


def test_probe_usage_failure_is_recorded_not_raised(conn):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    out = quick.probe_usage(
        Settings(), conn, runner=boom, claude_bin=Path("claude"), now=lambda: NOW
    )
    assert "error" in out
    assert conn.execute("SELECT status FROM agent_calls").fetchone()[0] == "error"
