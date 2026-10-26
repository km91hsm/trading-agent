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
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 14))
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 5))
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 14))  # 멱등
    m = pit.members(conn, "KR").set_index("ticker")
    assert m.loc["A", "first_seen"] == date(2026, 9, 5) and m.loc["A", "last_seen"] == date(
        2026, 9, 14
    )
    assert m.loc["B", "last_seen"] == date(2026, 9, 10)


def test_reentry_opens_new_interval_not_backfill(conn):
    # A 가 9/1~9/3 구성원 → 편출 → 9/30 재편입: 9/15 행은 구성원이 아니다
    for d in (1, 2, 3):
        pit.record(conn, market="KR", tickers=["A", "B"], day=date(2026, 9, d))
    for d in range(4, 31):
        pit.record(conn, market="KR", tickers=["B"], day=date(2026, 9, d))
    pit.record(conn, market="KR", tickers=["A"], day=date(2026, 9, 30))
    assert len(pit.members(conn, "KR").query("ticker == 'A'")) == 2
    out, _ = pit.apply(_df(), conn, PitCfg())
    a_days = set(out[out.ticker == "A"]["date"].dt.day)
    assert 15 not in a_days and 30 in a_days and 2 in a_days


def test_liquidity_rolling_ignores_input_order_and_other_markets():
    rows = [
        {
            "date": pd.Timestamp("2026-09-02"),
            "ticker": "A",
            "market": "KR",
            "close": 10.0,
            "volume": 100,
        },
        {
            "date": pd.Timestamp("2026-09-01"),
            "ticker": "A",
            "market": "KR",
            "close": 1.0,
            "volume": 1,
        },
        {
            "date": pd.Timestamp("2026-09-01"),
            "ticker": "B",
            "market": "KR",
            "close": 10.0,
            "volume": 10,
        },
        {
            "date": pd.Timestamp("2026-09-02"),
            "ticker": "B",
            "market": "KR",
            "close": 10.0,
            "volume": 10,
        },
        # 다른 시장의 같은 코드 A 는 거래대금이 커도 KR 의 A 와 섞이지 않는다
        {
            "date": pd.Timestamp("2026-08-31"),
            "ticker": "A",
            "market": "US",
            "close": 1e6,
            "volume": 1e6,
        },
    ]
    out, _ = pit.apply(pd.DataFrame(rows), None, PitCfg(liquidity_top_n=1, liquidity_window=5))
    kr = out[out.market == "KR"]
    assert kr[kr["date"] == "2026-09-01"]["ticker"].tolist() == ["B"]


def test_effective_day_after_close_is_next_day():
    from datetime import UTC, datetime

    # 10/27 06:40 KST = 뉴욕 10/26 17:40(마감 뒤) → 10/27 부터, KR 은 장 전이라 당일
    now = datetime(2026, 10, 26, 21, 40, tzinfo=UTC)
    assert pit.effective_day("US", now) == date(2026, 10, 27)
    assert pit.effective_day("KR", now) == date(2026, 10, 27)
    assert pit.effective_day("KR", datetime(2026, 10, 27, 7, 0, tzinfo=UTC)) == date(2026, 10, 28)


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
