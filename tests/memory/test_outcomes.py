from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
from helpers_ml import trading_days

from trader.ledger import repo
from trader.memory import outcomes, store
from trader.ml import features as feat

D0 = date(2026, 9, 1)
DAYS = trading_days(30, start=D0)
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _store(prices: dict[str, list[float]]):
    rows = []
    for t, closes in prices.items():
        for d, c in zip(DAYS[: len(closes)], closes, strict=True):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": c, "volume": 1})
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    feat.write_rows(df[feat.FEATURE_COLUMNS])


def _decision(conn, i, ticker, action, decided_day: date):
    from helpers_agents import ok_decision

    dec_id = repo.ingest_checkpoint(
        conn,
        decided_day,
        "KR",
        ticker,
        "30_decision",
        {"data": ok_decision(ticker=ticker, action=action)},
    )
    return store.record_decision(
        conn,
        decision_id=dec_id,
        ticker=ticker,
        market="KR",
        account_id="paper-main",
        decided_at=datetime(
            decided_day.year, decided_day.month, decided_day.day, 6, tzinfo=UTC
        ).isoformat(),
        decision={"action": action},
        row=None,
        horizon_days=5,
    )


def test_mature_fills_return_and_alpha_from_feature_store(conn, project):
    # A: 100 → 104 (5거래일 뒤 +4%), B: 100 → 98 (−2%) → 1/N 벤치마크 +1%
    _store({"A": [100] + [100] * 4 + [104] + [104] * 24, "B": [100] * 5 + [98] + [98] * 24})
    eid = _decision(conn, 1, "A", "BUY", DAYS[0])
    r = outcomes.mature(conn, state=None, today=DAYS[10])
    assert len(r) == 1 and r[0]["entry_id"] == eid
    e = store.get_entry(conn, eid)
    assert abs(e["realized_return"] - 0.04) < 1e-9 and abs(e["alpha_vs_benchmark"] - 0.03) < 1e-9
    assert e["outcome_at"] and e["tier"] == "short"
    assert outcomes.mature(conn, state=None, today=DAYS[10]) == []  # 멱등


def test_mature_skips_unmatured_and_without_prices(conn, project):
    _store({"A": [100] * 30})
    early = _decision(conn, 1, "A", "BUY", DAYS[27])  # 만기 전
    nopx = _decision(conn, 2, "Z", "BUY", DAYS[0])  # 종가 없음
    assert outcomes.mature(conn, state=None, today=DAYS[29]) == []
    assert store.get_entry(conn, early)["outcome_at"] is None
    assert store.get_entry(conn, nopx)["outcome_at"] is None


def test_sell_and_hold_decisions(conn, project):
    _store({"A": [100] * 5 + [90] * 25, "B": [100] * 5 + [110] * 25})
    sell = _decision(conn, 1, "A", "SELL", DAYS[0])
    hold = _decision(conn, 2, "B", "HOLD", DAYS[0])
    outcomes.mature(conn, state=None, today=DAYS[10])
    s = store.get_entry(conn, sell)
    assert abs(s["realized_return"] - 0.10) < 1e-9  # 매도: 하락이 이익 (부호 반전)
    h = store.get_entry(conn, hold)
    # HOLD: 실현수익 0, 알파 = −벤치마크 (A −10%·B +10% → 1/N 0% → 알파 0)
    assert h["realized_return"] == 0.0 and abs(h["alpha_vs_benchmark"]) < 1e-9
