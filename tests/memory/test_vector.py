import math

from trader.memory import vector


def test_situation_vector_12_dims_and_nan_safe():
    row = {
        "roc_10": 5.0,
        "rsi_14": 75.0,
        "vol_z_20": 1.5,
        "high_52w_pos": 0.9,
        "atr_14": 2.0,
        "close": 100.0,
        "macd_hist": 0.5,
        "sma_5": 102.0,
        "sma_20": 98.0,
        "flow_foreign_20d": -300,
        "news_count": 12,
        "community_count": None,
    }
    v = vector.situation_vector(row, "KR")
    assert len(v) == 12 == len(vector.DIMS) and all(isinstance(x, float) for x in v)
    assert all(math.isfinite(x) for x in v)
    d = dict(zip(vector.DIMS, v, strict=True))
    assert d["rsi_14"] == 0.5 and d["is_us"] == 0.0 and d["flow_foreign_sign"] == -1.0
    assert abs(d["atr_pct"] - 0.02) < 1e-9 and abs(d["sma_5_rel"] - 0.02) < 1e-9
    assert d["community_log"] == 0.0 and 0 < d["news_log"] <= 1.0
    assert vector.situation_vector({}, "US")[-1] == 1.0
    assert vector.cosine(v, v) > 0.999 and vector.cosine([0.0] * 12, v) == 0.0
    assert "RSI" in vector.summary(row, "KR") and "52주" in vector.summary(row, "KR")
