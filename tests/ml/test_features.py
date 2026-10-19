from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from helpers_sources import FakeDart, FakeMarket, make_candles

from trader.broker.errors import KisApiError
from trader.config import Universe, UsSymbol
from trader.ml import features
from trader.sources import indicators


def test_feature_columns_shape():
    assert features.FEATURE_COLUMNS[:3] == ["date", "ticker", "market"]
    assert set(indicators.COLUMNS) <= set(features.FEATURE_COLUMNS)
    for c in (
        "flow_foreign_20d",
        "op_margin",
        "roe",
        "debt_ratio",
        "news_count",
        "community_count",
    ):
        assert c in features.FEATURE_COLUMNS


def test_snapshot_row_columns():
    candles = make_candles(300)
    flows = FakeMarket().get_investor_kr("005930", days=20)
    row = features.snapshot_row(
        ticker="005930",
        market="KR",
        candles=candles,
        flows=flows,
        ratios={"영업이익률": 0.1, "ROE": 0.1, "부채비율": 0.25},
        news_count=3,
        community_count=5,
    )
    assert set(row) == set(features.FEATURE_COLUMNS)
    assert row["date"] == candles[-1].date and row["flow_foreign_20d"] == 2000
    assert row["op_margin"] == 0.1 and row["news_count"] == 3


def test_snapshot_row_missing_inputs_nan():
    row = features.snapshot_row(
        ticker="AAPL",
        market="US",
        candles=make_candles(10),
        flows=None,
        ratios=None,
        news_count=None,
        community_count=None,
    )
    assert row["sma_120"] is None and row["flow_foreign_20d"] is None and row["roe"] is None


def test_history_rows_forward_fills_ratios_from_april():
    candles = make_candles(600, start=date(2024, 1, 1))
    ratios_by_year = {
        2023: {"영업이익률": 0.05, "ROE": 0.06, "부채비율": 0.5},
        2024: {"영업이익률": 0.10, "ROE": 0.12, "부채비율": 0.4},
    }
    df = features.history_rows(
        ticker="005930", market="KR", candles=candles, ratios_by_year=ratios_by_year
    )
    assert list(df.columns) == features.FEATURE_COLUMNS and len(df) == 600
    by_date = df.set_index("date")
    assert pd.isna(by_date.loc[date(2024, 2, 1), "roe"])  # 2023 사업보고서 전
    assert by_date.loc[date(2024, 4, 1), "roe"] == 0.06  # 2023 값은 2024-04-01 부터
    assert by_date.loc[date(2025, 4, 1), "roe"] == 0.12  # 2024 값은 2025-04-01 부터
    assert by_date.loc[date(2025, 3, 31), "roe"] == 0.06
    assert pd.isna(by_date["flow_foreign_20d"]).all()


def test_write_rows_idempotent(project):
    candles = make_candles(3)
    df = pd.concat(
        [
            features.history_rows(
                ticker="005930", market="KR", candles=candles, ratios_by_year=None
            ),
            features.history_rows(
                ticker="000660", market="KR", candles=candles, ratios_by_year=None
            ),
        ]
    )
    paths = features.write_rows(df)
    features.write_rows(df)
    assert len(paths) == 3
    got = pd.read_parquet(project / "state" / "features" / f"{candles[0].date.isoformat()}.parquet")
    assert len(got) == 2 and set(got["ticker"]) == {"005930", "000660"}


def test_write_rows_dtypes_stable_across_paths(project):
    """snapshot·backfill 행을 같은 날짜 파일에 섞어 써도 dtype 이 고정된다 (리뷰 I3)."""
    candles = make_candles(3)
    hist = features.history_rows(ticker="005930", market="KR", candles=candles, ratios_by_year=None)
    features.write_rows(hist)
    row = features.snapshot_row(
        ticker="000660",
        market="KR",
        candles=candles,
        flows=None,
        ratios=None,
        news_count=None,
        community_count=None,
    )
    features.write_rows(pd.DataFrame([row])[features.FEATURE_COLUMNS])
    got = pd.read_parquet(
        project / "state" / "features" / f"{candles[-1].date.isoformat()}.parquet"
    )
    assert len(got) == 2
    for col in ("sma_120", "roe", "news_count", "flow_foreign_20d"):
        assert str(got[col].dtype) == "Float64", (col, got[col].dtype)
    assert str(got["close"].dtype) == "float64" and str(got["volume"].dtype) == "int64"
    assert str(got["ticker"].dtype) in ("object", "string", "str")


def test_backfill_continues_on_failure(project):
    class Market(FakeMarket):
        def get_daily_kr(self, ticker, days=250):
            if ticker == "000660":
                raise KisApiError("FAKE", "실패")
            return super().get_daily_kr(ticker, days)

    universe = Universe(kr=["005930", "000660"], us=[UsSymbol(symbol="AAPL", exchange="NAS")])
    n = features.backfill(
        market_client=Market(candles=40),
        dart=FakeDart(),
        universe=universe,
        markets=["KR", "US"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 80  # 005930 40행 + AAPL 40행
    files = list((project / "state" / "features").glob("*.parquet"))
    assert len(files) == 40
    got = pd.concat(pd.read_parquet(f) for f in files)
    assert set(got["ticker"]) == {"005930", "AAPL"}
    assert got[got["ticker"] == "005930"]["roe"].notna().any()
    assert got[got["ticker"] == "AAPL"]["roe"].isna().all()  # US 비율은 Phase 7


def test_backfill_continues_when_dart_lookup_fails(project):
    """실기 확인: DART 점검(status 800)으로 고유번호 조회가 실패해도 가격 지표 행은 저장한다."""

    class BrokenDart(FakeDart):
        def corp_code(self, ticker):
            raise RuntimeError("DART status 800")

    n = features.backfill(
        market_client=FakeMarket(candles=5),
        dart=BrokenDart(),
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 5
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    assert got["roe"].isna().all() and got["close"].notna().all()


def test_backfill_caps_at_1000_days(project):
    m = FakeMarket(candles=10)
    features.backfill(
        market_client=m,
        dart=None,
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        years=10,
        progress=lambda s: None,
    )
    assert m.calls == ["get_daily_kr"]


def test_snapshot_rejects_past_asof(project):
    from datetime import timedelta

    from helpers_sources import T0

    from trader.config import ConfigError

    with pytest.raises(ConfigError, match="과거"):
        features.snapshot(
            market_client=FakeMarket(),
            dart=None,
            universe=Universe(kr=["005930"]),
            markets=["KR"],
            asof=T0 - timedelta(days=2),
            now=lambda: T0,
        )


def test_snapshot_writes_today(project):
    from helpers_sources import T0

    n = features.snapshot(
        market_client=FakeMarket(),
        dart=FakeDart(),
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
    )
    assert n == 1
    files = list((project / "state" / "features").glob("*.parquet"))
    assert len(files) == 1
    got = pd.read_parquet(files[0])
    assert got.iloc[0]["flow_foreign_20d"] == 2000 and got.iloc[0]["roe"] == pytest.approx(0.1)
    assert Decimal(str(got.iloc[0]["close"])) > 0


# --- 이연 회수: 최신 행 부분 읽기 -----------------------------------------------------


def _rows(day: str, tickers: list[str]) -> pd.DataFrame:
    import numpy as np

    df = pd.DataFrame(
        [
            {
                "date": date.fromisoformat(day),
                "ticker": t,
                "market": "KR",
                "close": 100.0,
                "volume": 1,
            }
            for t in tickers
        ]
    )
    for c in features.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df[features.FEATURE_COLUMNS]


def test_latest_rows_falls_back_to_older_files(project):
    features.write_rows(_rows("2026-10-01", ["A", "B"]))
    features.write_rows(_rows("2026-10-02", ["A"]))
    df = features.latest_rows(None, "KR", tickers=["A", "B"])
    got = {
        t: pd.Timestamp(d).date().isoformat() for t, d in zip(df["ticker"], df["date"], strict=True)
    }
    assert got == {"A": "2026-10-02", "B": "2026-10-01"}
    capped = features.latest_rows(None, "KR", asof=date(2026, 10, 1))
    assert set(pd.to_datetime(capped["date"]).dt.date) == {date(2026, 10, 1)}
    assert features.latest_rows(None, "US").empty
    only = features.latest_rows(None, "KR", tickers=["A"], max_files=1)
    assert list(only["ticker"]) == ["A"]


def test_latest_rows_fills_columns_missing_in_old_files(project):
    old = _rows("2026-10-01", ["A"]).drop(columns=["sma_5_rel", "bb_pos"])
    path = features._features_dir(None) / "2026-10-01.parquet"
    old.to_parquet(path, index=False)  # Phase 8 이전에 쓰인 파일처럼 열이 빠져 있다
    df = features.latest_rows(None, "KR")
    assert list(df.columns) == list(features.FEATURE_COLUMNS)
    assert pd.isna(df["sma_5_rel"].iloc[0])


def test_latest_rows_keeps_feature_dtypes(project):
    features.write_rows(_rows("2026-10-01", ["A", "B"]))
    df = features.latest_rows(None, "KR")
    assert str(df["sma_5"].dtype) == "Float64" and str(df["close"].dtype) == "float64"
    assert str(df["volume"].dtype) == "int64"


def test_write_rows_tolerates_old_schema_file(project):
    """이전 Phase 파일은 열이 적다.

    latest_rows 는 reindex 로 메우지만 write_rows 가 KeyError 를 내면 snapshot 전체가 실패한다."""
    candles = make_candles(1)
    day = candles[0].date.isoformat()
    d = project / "state" / "features"
    d.mkdir(parents=True, exist_ok=True)
    old = pd.DataFrame(
        {
            "date": [candles[0].date],
            "ticker": ["000660"],
            "market": ["KR"],
            "close": [1.0],
            "volume": [10],
        }
    )
    old.to_parquet(d / f"{day}.parquet", index=False)
    df = features.history_rows(ticker="005930", market="KR", candles=candles, ratios_by_year=None)
    features.write_rows(df)
    got = pd.read_parquet(d / f"{day}.parquet")
    assert set(got["ticker"]) == {"000660", "005930"}
    assert pd.isna(got.loc[got["ticker"] == "000660", "sma_5"]).all()


def test_backfill_writes_in_chunks_not_per_ticker(project, monkeypatch):
    """종목마다 write_rows 를 부르면 날짜 파일을 종목 × 날짜 만큼 읽고 다시 쓴다(30종목 × 750일 ≈
    22,500회). 종목 프레임을 모아 묶음으로 쓴다."""
    calls = {"n": 0}
    real = features.write_rows

    def counting(df, state=None):
        calls["n"] += 1
        return real(df, state)

    monkeypatch.setattr(features, "write_rows", counting)
    universe = Universe(kr=[f"0000{i:02d}" for i in range(25)])
    n = features.backfill(
        market_client=FakeMarket(candles=5),
        dart=None,
        universe=universe,
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 125 and calls["n"] <= 3  # CHUNK=10 → 3회
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    assert len(got) == 125 and got["ticker"].nunique() == 25


def test_backfill_keeps_rows_when_chunk_write_fails_then_retries(project, monkeypatch):
    """묶음 쓰기가 실패해도 모은 행을 버리지 않고 다음 flush 에서 다시 쓴다. 마지막 flush 실패는
    예외로 끝내지 않고 로그만 남긴다 (수 분 걸린 백필이 비정상 종료되지 않도록)."""
    real = features.write_rows
    calls = {"n": 0}

    def flaky(df, state=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(df, state)

    monkeypatch.setattr(features, "write_rows", flaky)
    tickers = [f"{i:06d}" for i in range(1, 12)]  # 11종목 → 10종목에서 1차 flush, 마지막에 2차
    n = features.backfill(
        market_client=FakeMarket(candles=5),
        dart=None,
        universe=Universe(kr=tickers),
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert calls["n"] == 2 and n == 55  # 1차 실패분(10종목)도 2차에서 함께 저장된다
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    assert set(got["ticker"]) == set(tickers)


def test_backfill_final_flush_failure_does_not_raise(project, monkeypatch):
    def boom(df, state=None):
        raise OSError("disk full")

    monkeypatch.setattr(features, "write_rows", boom)
    n = features.backfill(
        market_client=FakeMarket(candles=5),
        dart=None,
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 0
