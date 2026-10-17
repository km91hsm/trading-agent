import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from trader.broker.models import Candle
from trader.sources import indicators

EXPECTED_COLUMNS = {
    "sma_5",
    "sma_20",
    "sma_60",
    "sma_120",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "atr_14",
    "roc_10",
    "vol_z_20",
    "high_52w_pos",
}


def candles(closes, start=date(2025, 1, 1)):
    out = []
    d = start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        c = Decimal(str(c))
        out.append(Candle(date=d, open=c - 1, high=c + 2, low=c - 2, close=c, volume=1000 + i))
        d += timedelta(days=1)
    return out


def test_frame_has_all_columns():
    df = indicators.indicator_frame(indicators.to_frame(candles([10] * 300)))
    assert EXPECTED_COLUMNS <= set(df.columns)
    assert len(df) == 300


def test_sma_rsi_known_values():
    closes = list(range(1, 31))  # 1..30 단조 상승
    latest = indicators.latest(candles(closes))
    assert latest["sma_5"] == pytest.approx(28.0)
    assert latest["sma_20"] == pytest.approx(20.5)
    assert latest["rsi_14"] == pytest.approx(100.0)
    assert latest["roc_10"] == pytest.approx((30 - 20) / 20)
    assert latest["sma_60"] is None and latest["sma_120"] is None


def test_insufficient_history_returns_none():
    latest = indicators.latest(candles([10, 11, 12]))
    assert latest["sma_5"] is None and latest["high_52w_pos"] is None
    assert latest["rsi_14"] is None and latest["atr_14"] is None
    assert all(v is None or not (isinstance(v, float) and math.isnan(v)) for v in latest.values())


def test_high_52w_pos_and_bollinger():
    closes = [10] * 251 + [20]  # 52주 고가 22(=20+2), 저가 8(=10-2), 종가 20
    latest = indicators.latest(candles(closes))
    assert latest["high_52w_pos"] == pytest.approx((20 - 8) / (22 - 8))
    assert latest["bb_upper"] > latest["bb_mid"] > latest["bb_lower"]
    assert latest["vol_z_20"] is not None


def test_empty_candles():
    assert indicators.latest([]) == {k: None for k in indicators.COLUMNS}


def test_derived_ratio_columns():
    """절대 가격 수준 지표를 close 대비 비율로 (Phase 7 이연): 종목 간 일반화용."""
    df = indicators.indicator_frame(indicators.to_frame(candles(list(range(100, 400)))))
    for c in ("sma_5_rel", "sma_20_rel", "sma_60_rel", "bb_pos", "atr_pct", "macd_hist_rel"):
        assert c in indicators.COLUMNS and c in df.columns
    last = df.iloc[-1]
    assert last["sma_20_rel"] == pytest.approx(last["close"] / last["sma_20"] - 1)
    assert last["sma_5_rel"] == pytest.approx(last["close"] / last["sma_5"] - 1)
    assert last["bb_pos"] == pytest.approx(
        (last["close"] - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"])
    )
    assert last["atr_pct"] == pytest.approx(last["atr_14"] / last["close"])
    assert last["macd_hist_rel"] == pytest.approx(last["macd_hist"] / last["close"])
    latest = indicators.latest(candles(list(range(100, 400))))
    assert latest["sma_60_rel"] == pytest.approx(last["close"] / last["sma_60"] - 1)
    assert indicators.latest(candles([10] * 5))["bb_pos"] is None
