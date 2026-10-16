from decimal import Decimal

import yaml
from conftest import make_account
from typer.testing import CliRunner

from trader import cli
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash
from trader.cli import app
from trader.ledger import db
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


def test_kis_ping_counts_account_without_keys(project, monkeypatch):
    """앱키가 없어 건너뛴 계좌는 침묵이 아니라 실패로 센다 (리뷰 Important 1)."""
    _accounts(project)
    market, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", lambda s, accs: brokers(s, accs)[:1])  # paper 만
    r = CliRunner().invoke(app, ["kis", "ping"])
    assert r.exit_code == 1
    assert "[실패] real-main" in r.stdout and "앱키" in r.stdout and "실패 1건" in r.stdout


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


def test_kis_order_test_paper_places_and_cancels(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes()
    built = {}

    def _brokers(secrets, accounts):
        bs = brokers(secrets, accounts)
        built.update({b.account.id: b for b in bs})
        return bs

    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", _brokers)
    r = CliRunner().invoke(app, ["kis", "order-test", "--account", "paper-main"])
    assert r.exit_code == 0, r.stdout
    b = built["paper-main"]
    assert len(b.orders) == 1 and b.orders[0].qty == 1 and b.orders[0].order_type == "LIMIT"
    assert b.orders[0].price == Decimal("64300")  # 71500 × 0.9 = 64350 → 호가단위 100 내림
    assert b.cancelled == ["FAKE-1"]
    assert "접수" in r.stdout and "취소" in r.stdout
    conn = db.connect()
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0  # 원장에 남기지 않음
    conn.close()


def test_kis_order_test_refuses_real(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "order-test", "--account", "real-main"])
    assert r.exit_code == 1 and "모의" in (r.stdout + (r.stderr or ""))


def test_kis_order_test_cancels_even_if_cancel_fails(project, monkeypatch):
    _accounts(project)
    market, brokers = _fakes(broker_fail=("cancel_order",))
    monkeypatch.setattr(cli, "_build_market", lambda s: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "order-test", "--account", "paper-main"])
    assert r.exit_code == 1 and "취소 실패" in r.stdout


def test_tool_ctx_builds_brokers_even_in_dry_run(project, monkeypatch):
    """dry_run 은 주문만 막고 판정은 실제 잔고로 한다 (리뷰 I5)."""
    _accounts(project)
    market, brokers = _fakes()
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.delenv("TRADER_DRY_RUN", raising=False)
    ctx = cli._tool_ctx()
    ctx.conn.close()
    assert ctx.dry_run is True and set(ctx.brokers) == {"paper-main", "real-main"}


def test_cli_fills_sync(project, monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal

    from trader.broker.models import Fill

    _accounts(project)
    market, brokers = _fakes()

    def _brokers(secrets, accounts):
        bs = brokers(secrets, accounts)
        for b in bs:
            if b.account.id == "paper-main":
                from zoneinfo import ZoneInfo

                kst_today = datetime.now(ZoneInfo("Asia/Seoul")).date()
                b.fills[("KR", kst_today)] = [
                    Fill(
                        order_no="Q",
                        ticker="005930",
                        market="KR",
                        side="BUY",
                        qty=1,
                        price=Decimal("70000"),
                        filled_at=datetime.now(UTC),
                    )
                ]
        return bs

    monkeypatch.setattr(cli, "_build_brokers", _brokers)
    r = CliRunner().invoke(app, ["fills", "sync", "--account", "paper-main"])
    assert r.exit_code == 0, r.stdout
    assert "paper-main" in r.stdout and "1" in r.stdout
    conn = db.connect()
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    conn.close()
