import json
from datetime import date, timedelta
from decimal import Decimal

from typer.testing import CliRunner

from trader import cli
from trader.broker.fake import FakeMarket
from trader.broker.models import Candle
from trader.cli import app


def test_regime_show_and_backtest(project, monkeypatch):
    r = CliRunner().invoke(app, ["regime", "show"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["hedge"]["max_weight_pct"] == 0.2 and out["markets"][0]["state"] is None
    closes = [100.0] * 230 + [100.0 - i for i in range(1, 41)] + [60.0] * 30
    d0 = date(2025, 1, 1)
    candles = [
        Candle(date=d0 + timedelta(days=i), open=c, high=c, low=c, close=Decimal(str(c)), volume=1)
        for i, c in enumerate(closes)
    ]
    monkeypatch.setattr(cli, "_build_market", lambda s: FakeMarket(candles={"069500": candles}))
    r = CliRunner().invoke(app, ["regime", "backtest", "--market", "KR", "--days", "300"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["proxy"] == "069500" and out["candles"] == 300 and out["transitions"] >= 1
    assert out["bear_spans"][0]["max_count"] >= 2
    r = CliRunner().invoke(app, ["regime", "backtest", "--market", "JP"])
    assert r.exit_code == 1
