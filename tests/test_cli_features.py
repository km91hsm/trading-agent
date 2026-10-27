import pytest
import yaml
from helpers_sources import FakeDart, FakeMarket
from typer.testing import CliRunner

from trader import cli, config
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
    assert seen.get("extra_targets") is None
    # 2-L1: 열린 창 종목은 extra_targets 로 유니버스에 더해진다
    assert run(["US"], extra=[("RIVN", "US", "NAS")]) == 1
    assert seen["extra_targets"] == [("RIVN", "US", "NAS")]


def test_daemon_features_fn_dispatches_actions(project, monkeypatch):
    from trader.ml import features

    seen: list = []
    monkeypatch.setattr(cli, "_build_dart", lambda s: "DART")
    monkeypatch.setattr(cli, "_build_edgar", lambda: "EDGAR")
    monkeypatch.setattr(features, "backfill", lambda **kw: seen.append(("backfill", kw)) or 3)
    monkeypatch.setattr(features, "snapshot", lambda **kw: seen.append(("snapshot", kw)) or 2)
    monkeypatch.setattr(features, "rederive", lambda **kw: seen.append(("derive", kw)) or 1)
    run = cli._daemon_features_fn(object(), "UNI", market_client="MKT")
    assert run("backfill", ["KR"], years=2) == 3
    assert (
        seen[0][1]["years"] == 2 and seen[0][1]["dart"] == "DART" and seen[0][1]["edgar"] == "EDGAR"
    )
    assert run("snapshot", ["US"]) == 2 and seen[1][1]["markets"] == ["US"]
    assert run("derive", []) == 1
    # 시세 클라이언트가 없으면 derive 만 가능
    none = cli._daemon_features_fn(object(), "UNI", market_client=None)
    assert none("derive", []) == 1
    with pytest.raises(config.ConfigError):
        none("backfill", ["KR"], years=1)
