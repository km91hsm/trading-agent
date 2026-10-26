"""시점 기준 유니버스(Stock D-037): 기록 시작 뒤에는 그날 구성원만, 편출 종목의 과거 행은 남기고,
strict 면 기록 전 날짜를 빼며, 거래대금 상위 N 은 그날까지의 값으로만."""

from datetime import date

import pandas as pd

from trader.config import PitCfg
from trader.ml import universe_pit as pit


def _df():
    rows = []
    for d in pd.bdate_range("2026-09-01", "2026-09-30"):
        for t, vol in (("A", 100), ("B", 10), ("C", 1)):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": 10.0, "volume": vol})
    return pd.DataFrame(rows)


def test_record_is_idempotent_and_extends(conn):
    assert pit.record(conn, market="KR", tickers=["A", "B"], day=date(2026, 9, 10)) == 2
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 20))
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 5))
    m = pit.members(conn, "KR").set_index("ticker")
    assert m.loc["A", "first_seen"] == date(2026, 9, 5) and m.loc["A", "last_seen"] == date(
        2026, 9, 20
    )
    assert m.loc["B", "last_seen"] == date(2026, 9, 10)


def test_apply_membership_coverage_and_strict(conn):
    # A 는 계속, B 는 9/10 까지만(편출), C 는 기록된 적 없음(오늘 유니버스 밖)
    for d in pd.bdate_range("2026-09-08", "2026-09-30"):
        pit.record(conn, market="KR", tickers=["A"] + (["B"] if d.day <= 10 else []), day=d.date())
    df = _df()
    out, info = pit.apply(df, conn, PitCfg())
    kr = info["markets"]["KR"]
    assert kr["recorded_since"] == "2026-09-08"
    after = out[out["date"] >= "2026-09-08"]
    assert set(after["ticker"]) == {"A", "B"}
    assert after[after.ticker == "B"]["date"].max() <= pd.Timestamp("2026-09-15")  # 5일 유예
    before = out[out["date"] < "2026-09-08"]
    assert set(before["ticker"]) == {"A", "B", "C"}  # 기록 전은 그대로(strict 아님)
    assert 0 < info["coverage"] < 1
    strict, _ = pit.apply(df, conn, PitCfg(strict=True))
    assert strict["date"].min() >= pd.Timestamp("2026-09-08")


def test_no_records_noop_unless_strict(conn):
    df = _df()
    out, info = pit.apply(df, conn, PitCfg())
    assert len(out) == len(df) and info["coverage"] == 0.0
    assert pit.apply(df, conn, PitCfg(strict=True))[0].empty


def test_liquidity_top_n_uses_past_only():
    df = _df()
    # 9/15 부터 C 의 거래량이 폭증 — 그 전에는 상위 1 에 들 수 없다
    df.loc[(df.ticker == "C") & (df["date"] >= "2026-09-15"), "volume"] = 10_000
    out, _ = pit.apply(df, None, PitCfg(liquidity_top_n=1, liquidity_window=5))
    early = out[out["date"] < "2026-09-15"]
    assert set(early["ticker"]) == {"A"}
    assert "C" in set(out[out["date"] >= "2026-09-20"]["ticker"])
