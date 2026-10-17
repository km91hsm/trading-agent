from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx
import yaml
from helpers_sources import FakeDart, FakeMarket
from typer.testing import CliRunner

from trader import cli
from trader.cli import app
from trader.ledger import db
from trader.orchestrator.session import SessionResult
from trader.sources.inputs import InputDeps

_ASOF_DT = datetime.now(UTC) - timedelta(hours=1)  # 24시간 창 안의 과거 시각
ASOF = _ASOF_DT.isoformat()
DAY = _ASOF_DT.astimezone(ZoneInfo("Asia/Seoul")).date()
DAY_S = DAY.isoformat()
REPO_AGENTS = Path(__file__).resolve().parents[1] / ".claude" / "agents"


@pytest.fixture
def quiet_project(project, monkeypatch):
    (project / "config" / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "edgar": {"user_agent": "trader test@example.com"},
                "news": {"enabled": False},
                "macro": {"enabled": False},
                "community": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": "paper-main",
                        "env": "paper",
                        "cano": "1",
                        "markets": ["KR", "US"],
                        "mode": "auto",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # 에이전트 md 는 저장소 것을 복사
    agents = project / ".claude" / "agents"
    agents.mkdir(parents=True)
    for f in REPO_AGENTS.glob("*.md"):
        (agents / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_build_input_deps",
        lambda s, c: InputDeps(market=FakeMarket(), dart=FakeDart(), edgar=None),
    )
    with respx.mock(assert_all_called=False):
        yield project


def _run(args):
    return CliRunner().invoke(app, ["analyze", *args])


def test_cli_analyze_fake_creates_decision(quiet_project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "결정: HOLD" in r.stdout and "30_decision.json" in r.stdout and "FAKE" in r.stdout
    d = quiet_project / "state" / "runs" / DAY_S / "KR" / "005930"
    assert (d / "30_decision.json").exists() and (d / "00_inputs.json").exists()
    conn = db.connect()
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0] == 1


def test_rerun_only_missing_stages(quiet_project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    assert _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"]).exit_code == 0
    d = quiet_project / "state" / "runs" / DAY_S / "KR" / "005930"
    (d / "20_debate_r1_bear.json").unlink()
    (d / "30_decision.json").unlink()
    r = _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "새로 쓴 단계: 20_debate_r1_bear, 25_research_summary, 30_decision" in r.stdout


def test_cli_analyze_without_dry_run_executes_via_fake_broker(quiet_project, monkeypatch):
    """--dry-run 없이 FAKE 세션: 판정이 dry_run=False 로 기록되고 실전(off)은 주문 기록이 없다."""

    from trader.orchestrator.run_store import read_checkpoint, run_dir

    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = _run(["--ticker", "005930", "--asof", ASOF])
    assert r.exit_code == 0, r.stdout
    d = run_dir(DAY, "KR", "005930")
    rc = read_checkpoint(d, "40_risk_check_paper-main")
    assert rc is not None and rc["data"]["dry_run"] is False
    assert rc["data"]["approved"] is False and "HOLD" in rc["data"]["reasons"][0]  # FAKE 는 HOLD
    assert read_checkpoint(d, "50_order_real-main") is None


def test_cli_analyze_dry_run_writes_verdict_without_order(quiet_project, monkeypatch):

    from trader.orchestrator.run_store import read_checkpoint, run_dir

    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"])
    assert r.exit_code == 0, r.stdout
    d = run_dir(DAY, "KR", "005930")
    assert read_checkpoint(d, "40_risk_check_paper-main")["data"]["dry_run"] is True
    assert read_checkpoint(d, "50_order_paper-main") is None


def test_cli_analyze_skip_collect_without_inputs_fails(quiet_project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = _run(["--ticker", "005930", "--asof", ASOF, "--dry-run", "--skip-collect"])
    assert r.exit_code == 1 and "00_inputs" in (r.stdout + (r.stderr or ""))


def test_cli_analyze_session_failure_exit1(quiet_project, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    monkeypatch.setattr(
        cli,
        "run_session",
        lambda *a, **k: SessionResult(ok=False, session_id="s", error="budget exceeded"),
    )
    r = _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"])
    assert r.exit_code == 1 and "budget" in (r.stdout + (r.stderr or ""))


def test_cli_analyze_us_requires_exchange(quiet_project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = _run(["--ticker", "AAPL", "--market", "US", "--asof", ASOF, "--dry-run"])
    assert r.exit_code == 1


def test_status_shows_last_decisions(quiet_project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    assert _run(["--ticker", "005930", "--asof", ASOF, "--dry-run"]).exit_code == 0
    r = CliRunner().invoke(app, ["status"])
    assert r.exit_code == 0
    text = (quiet_project / "state" / "STATUS.md").read_text(encoding="utf-8")
    assert "## 최근 결정" in text and "005930" in text and "HOLD" in text
    assert "오늘 LLM 호출" in text and "0건" in text  # FAKE 세션은 집계 제외
