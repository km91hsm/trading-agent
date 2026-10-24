"""데몬 ↔ 국면 판정·헤지: scan_analyze 앞 regime 갱신, monitor 첫 틱 헤지 1회, paused 보류."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from conftest import make_account
from test_daemon import _deps

from trader import daemon
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Candle, Cash
from trader.config import HedgeCfg, Settings
from trader.control import control
from trader.orchestrator import hedge
from trader.risk import regime

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # 10:00 KST (KR monitor 창 안)
CFG = HedgeCfg(ma_days=20, momentum_days=5, exit_streak_days=1)


def _candles(closes):
    d0 = date(2026, 9, 1)
    return [
        Candle(date=d0 + timedelta(days=i), open=c, high=c, low=c, close=Decimal(str(c)), volume=1)
        for i, c in enumerate(closes)
    ]


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def _hedge_deps(conn, *, closes, prices=None):
    market = FakeMarket(prices=prices or {}, candles={"069500": _candles(closes)}, now=lambda: NOW)
    deps = _deps(conn, market, now=lambda: NOW)
    deps.settings = Settings(hedge=CFG)
    deps.notifier = Notes()
    acct = make_account("paper-main", "paper", "auto", ["KR", "US"])
    deps.brokers = {
        "paper-main": FakeBroker(
            acct,
            cash={
                "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))
            },
        )
    }
    deps.accounts = [acct]
    return deps


def test_regime_body_updates_flag_and_notifies_transition(conn):
    deps = _hedge_deps(conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70])
    out = daemon._regime_body(deps, "KR")
    assert out["state"] == "bear" and out["count"] == 2
    assert regime.load(conn, "KR")["state"] == "bear"
    assert any("[국면 전환] KR - → bear" in m for m in deps.notifier.msgs)
    # 데이터가 없으면 error 로 보고하고 직전 상태 유지
    deps.market_client.candles = {}
    assert "error" in daemon._regime_body(deps, "KR") and regime.load(conn, "KR")["state"] == "bear"
    deps.settings = Settings(hedge=HedgeCfg(enabled=False))
    assert daemon._regime_body(deps, "KR") == {"skipped": "hedge.enabled=false"}


def test_monitor_body_runs_hedge_once_per_day(conn, monkeypatch):
    deps = _hedge_deps(
        conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70], prices={"114800": Decimal("5000")}
    )
    daemon._regime_body(deps, "KR")
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 0})
    out = daemon._monitor_body(deps, "KR")
    assert out["hedge"]["action"] == "enter" and out["hedge"]["ticker"] == "114800"
    orders = deps.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].ticker == "114800" and orders[0].qty == 400
    assert control.get_flag(conn, hedge.done_key("KR", date(2026, 10, 24)))
    out2 = daemon._monitor_body(deps, "KR")
    assert "hedge" not in out2 and len(deps.brokers["paper-main"].orders) == 1


def test_monitor_body_hedge_paused_notifies_once(conn, monkeypatch):
    deps = _hedge_deps(
        conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70], prices={"114800": Decimal(1)}
    )
    daemon._regime_body(deps, "KR")
    control.set_flag(conn, "paused", True)
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 0})
    daemon._monitor_body(deps, "KR")
    daemon._monitor_body(deps, "KR")
    assert sum("[헤지 보류: 일시정지]" in m for m in deps.notifier.msgs) == 1
    assert deps.brokers["paper-main"].orders == []


def test_hedge_failure_does_not_block_monitor(conn, monkeypatch):
    deps = _hedge_deps(conn, closes=[100] * 30)
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 7})

    def boom(d, m):
        raise RuntimeError("x")

    monkeypatch.setattr(daemon, "_hedge_body", boom)
    out = daemon._monitor_body(deps, "KR")
    assert out["checked"] == 7 and out["hedge"]["error"].startswith("RuntimeError")
