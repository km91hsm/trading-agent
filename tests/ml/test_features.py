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
