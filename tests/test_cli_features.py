import yaml
from helpers_sources import FakeDart, FakeMarket
from typer.testing import CliRunner

from trader import cli
from trader.cli import app


def _universe(project):
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930"], "us": [{"symbol": "AAPL", "exchange": "NAS"}]}),
        encoding="utf-8",
    )


def test_cli_features_backfill(project, monkeypatch):
    _universe(project)
    monkeypatch.setattr(cli, "_build_market", lambda s: FakeMarket(candles=30))
    monkeypatch.setattr(cli, "_build_dart", lambda s: FakeDart())
    r = CliRunner().invoke(app, ["features", "backfill", "--years", "1", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    assert "30행" in r.stdout
    assert len(list((project / "state" / "features").glob("*.parquet"))) == 30


def test_cli_features_snapshot(project, monkeypatch):
    _universe(project)
    monkeypatch.setattr(cli, "_build_market", lambda s: FakeMarket())
    monkeypatch.setattr(cli, "_build_dart", lambda s: None)
    r = CliRunner().invoke(app, ["features", "snapshot"])
    assert r.exit_code == 0, r.stdout
    assert "2행" in r.stdout


def test_cli_features_market_build_failure_exit1(project, monkeypatch):
    _universe(project)

    def boom(s):
        raise RuntimeError("키 없음")

    monkeypatch.setattr(cli, "_build_market", boom)
    r = CliRunner().invoke(app, ["features", "snapshot"])
    assert r.exit_code == 1
