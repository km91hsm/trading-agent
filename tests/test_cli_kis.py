from decimal import Decimal

import yaml
from conftest import make_account
from typer.testing import CliRunner

from trader import cli
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash
from trader.cli import app
from trader.notify.telegram import NullNotifier


def _accounts(project):
    data = {
        "accounts": [
            {
                "id": "paper-main",
                "env": "paper",
                "cano": "00000000",
                "markets": ["KR", "US"],
                "mode": "auto",
            },
            {
                "id": "real-main",
                "env": "real",
                "cano": "11111111",
                "markets": ["KR"],
                "mode": "off",
            },
        ]
    }
    (project / "config" / "accounts.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _fakes(market_fail=(), broker_fail=()):
    market = FakeMarket(
        prices={"005930": Decimal("71500"), "AAPL": Decimal("227.55")}, fail=set(market_fail)
    )

    def brokers(secrets, accounts):
        return [
            FakeBroker(
                a,
                cash={
                    "KR": Cash(currency="KRW", available=1, total=1),
                    "US": Cash(currency="USD", available=1, total=1),
                },
                fail=set(broker_fail),
            )
            for a in accounts
        ]

    return market, brokers


def test_kis_ping_all_ok(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "ping"])
    assert r.exit_code == 0, r.stdout
    assert "005930" in r.stdout and "AAPL" in r.stdout and "paper-main" in r.stdout
    assert "실패 0건" in r.stdout


def test_kis_ping_reports_failures(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes(market_fail={"get_price_us"})
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "ping"])
    assert r.exit_code == 1
    assert "실패 1건" in r.stdout and "AAPL" in r.stdout


def test_kis_ping_account_filter(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "ping", "--account", "real-main"])
    assert r.exit_code == 0, r.stdout
    assert "real-main" in r.stdout and "paper-main" not in r.stdout


def test_kis_ping_market_build_failure_counts(project, monkeypatch):
    _accounts(project)

    def boom(secrets):
        raise RuntimeError("키 없음")

    _, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_market", boom)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "ping"])
    assert r.exit_code == 1 and "시세 클라이언트" in r.stdout


def test_notify_test_null(project, monkeypatch):
    monkeypatch.setattr(cli, "make_notifier", lambda s: NullNotifier())
    r = CliRunner().invoke(app, ["notify-test"])
    assert r.exit_code == 0 and "전송 성공" in r.stdout


def test_notify_test_failure(project, monkeypatch):
    class Bad:
        def send(self, text, *, escape=True):
            return False

    monkeypatch.setattr(cli, "make_notifier", lambda s: Bad())
    r = CliRunner().invoke(app, ["notify-test"])
    assert r.exit_code == 1


def test_build_brokers_skips_env_without_keys(project):
    from trader.config import Secrets

    secrets = Secrets(KIS_PAPER_APP_KEY="k", KIS_PAPER_APP_SECRET="s")
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    brokers = cli._build_brokers(secrets, accounts)
    assert [b.account.id for b in brokers] == ["paper-main"]
