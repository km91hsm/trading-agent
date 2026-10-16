import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import respx
import yaml
from helpers_sources import FakeDart, FakeMarket
from typer.testing import CliRunner

from trader import cli
from trader.cli import app
from trader.sources.inputs import InputDeps

_ASOF_DT = datetime.now(UTC) - timedelta(hours=1)  # 24시간 창 안의 과거 시각
ASOF = _ASOF_DT.isoformat()
DAY_S = _ASOF_DT.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()


@pytest.fixture
def quiet_project(project):
    """뉴스·거시·커뮤니티를 끈 sources.yaml 과 HTTP 차단 가드."""
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
    with respx.mock(assert_all_called=False):
        yield project


def test_cli_collect(quiet_project, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_build_input_deps",
        lambda s, c: InputDeps(market=FakeMarket(), dart=FakeDart(), edgar=None),
    )
    r = CliRunner().invoke(app, ["collect", "--ticker", "005930", "--asof", ASOF])
    assert r.exit_code == 0, r.stdout
    p = quiet_project / "state" / "runs" / DAY_S / "KR" / "005930" / "00_inputs.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["meta"]["name"] == "삼성전자" and d["fundamental"]["ratios"]["data"]["ROE"]
    assert "오류 절 0건" in r.stdout and str(p) in r.stdout


def test_cli_collect_partial_failure_exit0(quiet_project, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_build_input_deps",
        lambda s, c: InputDeps(
            market=FakeMarket(fail={"get_minute_kr"}), dart=FakeDart(), edgar=None
        ),
    )
    r = CliRunner().invoke(app, ["collect", "--ticker", "005930"])
    assert r.exit_code == 0 and "오류 절 1건" in r.stdout


def test_cli_collect_us_without_exchange_fails(quiet_project, monkeypatch):
    monkeypatch.setattr(
        cli, "_build_input_deps", lambda s, c: InputDeps(market=None, dart=None, edgar=None)
    )
    r = CliRunner().invoke(app, ["collect", "--ticker", "AAPL", "--market", "US"])
    assert r.exit_code == 1


def test_cli_collect_bad_market(quiet_project):
    r = CliRunner().invoke(app, ["collect", "--ticker", "X", "--market", "JP"])
    assert r.exit_code == 1


def test_cli_collect_reports_skip_when_existing(quiet_project, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_build_input_deps",
        lambda s, c: InputDeps(market=FakeMarket(), dart=FakeDart(), edgar=None),
    )
    args = ["collect", "--ticker", "005930", "--asof", ASOF]
    assert CliRunner().invoke(app, args).exit_code == 0
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0 and "건너뜀" in r.stdout
