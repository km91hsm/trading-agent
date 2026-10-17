from decimal import Decimal

from conftest import make_account
from typer.testing import CliRunner

from trader import cli
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash
from trader.cli import app


def test_cli_monitor_once(project, monkeypatch):
    import yaml

    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": "paper-main",
                        "env": "paper",
                        "cano": "0",
                        "markets": ["KR"],
                        "mode": "auto",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cash = {"KR": Cash(currency="KRW", available=Decimal(1), total=Decimal(1))}
    monkeypatch.setattr(
        cli,
        "_build_brokers",
        lambda s, a: [FakeBroker(make_account("paper-main", "paper", "auto"), cash=cash)],
    )
    monkeypatch.setattr(
        cli, "_build_market", lambda s: FakeMarket(prices={"005930": Decimal("70000")})
    )
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    r = CliRunner().invoke(app, ["monitor", "--market", "KR", "--once"])
    assert r.exit_code == 0, r.stdout
    assert '"checked": 0' in r.stdout
    assert CliRunner().invoke(app, ["monitor", "--market", "XX", "--once"]).exit_code == 1
