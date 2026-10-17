"""간이 백테스트 (스펙 §13): 컷오프 이후 결정만, 메모리 결과 우선·없으면 피처 스토어 종가로 계산."""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest
from helpers_memory import seed_matured
from helpers_ml import trading_days

from trader import backtest
from trader.memory import store
from trader.ml import features as feat


def _store(prices: dict[str, list[float]], days):
    rows = []
    for t, closes in prices.items():
        for d, c in zip(days, closes, strict=True):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": c, "volume": 1})
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    feat.write_rows(df[feat.FEATURE_COLUMNS])


def test_backtest_only_after_cutoff(project, conn):
    seed_matured(conn, 4, start=date(2026, 1, 5))  # 컷오프 이전
    seed_matured(conn, 3, start=date(2026, 3, 2), hit_rate=1.0)  # 컷오프 이후(결과 있음)
    # 컷오프 이후 결과 없는 결정 1건: 피처 스토어 종가(결정일 이후 봉)로 계산된다
    days = trading_days(10, start=date(2026, 4, 1))
    _store({"ZZZ": [100, 101, 102, 103, 104, 110, 111, 112, 113, 114]}, days)
    store.record_decision(
        conn,
        decision_id=9001,
        ticker="ZZZ",
        market="KR",
        account_id="paper-main",
        decided_at=datetime(2026, 4, 1, 6, tzinfo=UTC).isoformat(),
        decision={"action": "BUY", "confidence": 0.5},
        row=None,
        horizon_days=5,
        vector=[0.5] * 12,
    )
    r = backtest.simple_backtest(conn, project / "state", since=date(2026, 3, 1))
    assert r["cutoff"] == "2026-03-01"
    assert r["n"] == 4
    zz = [x for x in r["decisions"] if x["ticker"] == "ZZZ"][0]
    assert zz["realized_return"] == pytest.approx(0.10, abs=1e-6)
    assert zz["source"] == "features"


def test_backtest_hit_rate_and_alpha(project, conn):
    seed_matured(conn, 10, start=date(2026, 3, 2), hit_rate=0.7)
    r = backtest.simple_backtest(conn, project / "state", since=date(2026, 3, 1))
    assert r["n"] == 10
    assert r["hit_rate"] == pytest.approx(0.7)
    assert r["avg_return"] == pytest.approx((7 * 0.03 - 3 * 0.03) / 10)
    assert r["avg_alpha"] == pytest.approx((7 * 0.03 - 3 * 0.03) / 10 - 0.001)
    assert r["by_action"]["BUY"]["n"] == 10 and r["by_action"]["BUY"]["hit_rate"] == pytest.approx(
        0.7
    )
    assert backtest.simple_backtest(conn, project / "state", since=date(2027, 1, 1))["n"] == 0


def test_backtest_hold_hit_is_small_move(project, conn):
    from helpers_memory import seed_matured as _sm

    _sm(conn, 2, start=date(2026, 5, 4), action="HOLD")  # price_return ±0.03 → 밴드 밖
    r = backtest.simple_backtest(conn, None, since=date(2026, 5, 1))
    assert r["n"] == 2 and all(d["hit"] is False for d in r["decisions"])
    conn.execute("UPDATE memory_entries SET price_return=0.01")
    conn.commit()
    r = backtest.simple_backtest(conn, None, since=date(2026, 5, 1))
    assert all(d["hit"] is True for d in r["decisions"]) and "price_return" in r["decisions"][0]
