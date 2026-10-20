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
    monkeypatch.setattr(cli, "_build_edgar", lambda: None)  # sources.yaml 없는 임시 프로젝트
    r = CliRunner().invoke(app, ["features", "backfill", "--years", "1", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    assert "30행" in r.stdout
    assert len(list((project / "state" / "features").glob("*.parquet"))) == 30


def test_cli_features_snapshot(project, monkeypatch):
    _universe(project)
    monkeypatch.setattr(cli, "_build_market", lambda s: FakeMarket())
    monkeypatch.setattr(cli, "_build_dart", lambda s: None)
    monkeypatch.setattr(cli, "_build_edgar", lambda: None)
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


def test_daemon_snapshot_fn_passes_edgar(project, monkeypatch):
    """데몬 reflect 앞 스냅샷도 EDGAR 를 넘겨야 US 비율이 매일 NaN 으로 덮이지 않는다 (리뷰 #1)."""
    from trader.ml import features

    seen: dict = {}
    monkeypatch.setattr(cli, "_build_dart", lambda s: None)
    monkeypatch.setattr(cli, "_build_edgar", lambda: "EDGAR")
    monkeypatch.setattr(features, "snapshot", lambda **kw: seen.update(kw) or 1)
    run = cli._daemon_snapshot_fn(object(), object(), market_client=object())
    assert run(["US"]) == 1 and seen["edgar"] == "EDGAR"
