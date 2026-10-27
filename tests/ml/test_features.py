from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from helpers_sources import FakeDart, FakeEdgar, FakeMarket, make_candles

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
    assert got[got["ticker"] == "AAPL"]["roe"].isna().all()  # edgar 미주입이면 US 비율은 NaN


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


def test_latest_rows_columns_and_memo(project, monkeypatch):
    """전체 검토 3차(성능): columns 면 키 열+그 열만, 같은 입력의 반복 호출은 파일을 다시 읽지
    않고, 날짜 파일을 새로 쓰면(디렉터리 mtime) 다시 읽는다. 옛 파일에 없는 열은 NaN."""
    old = _rows("2026-10-01", ["A", "B"]).drop(columns=["vol_z_20"])
    old.to_parquet(features._features_dir(None) / "2026-10-01.parquet", index=False)
    reads: list[str] = []
    real = features.read_part

    def spy(path, *a, **k):
        reads.append(path.stem)
        return real(path, *a, **k)

    monkeypatch.setattr(features, "read_part", spy)
    df = features.latest_rows(None, "KR", tickers=["A", "Z"], columns=["vol_z_20"])
    assert list(df.columns) == [*features.KEY_COLUMNS, "vol_z_20"]
    assert list(df["ticker"]) == ["A"] and pd.isna(df["vol_z_20"].iloc[0])
    n = len(reads)
    df["close"] = -1.0  # 돌려준 값을 고쳐도 기억된 결과는 그대로
    again = features.latest_rows(None, "KR", tickers=["A", "Z"], columns=["vol_z_20"])
    assert len(reads) == n and again["close"].iloc[0] == 100.0
    features.write_rows(_rows("2026-10-02", ["Z"]))
    fresh = features.latest_rows(None, "KR", tickers=["A", "Z"], columns=["vol_z_20"])
    assert len(reads) > n and sorted(fresh["ticker"]) == ["A", "Z"]
    # latest_rows 와 같은 범위(최근 60개 파일)의 합집합 — 최신 파일만 보지 않는다
    assert features.known_tickers(None, "KR") == {"A", "B", "Z"}
    assert features.known_tickers(None, "KR", asof=date(2026, 10, 1)) == {"A", "B"}
    assert features.known_tickers(None, "US") == set()


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

    def counting(df, state=None, **kw):
        calls["n"] += 1
        return real(df, state, **kw)

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

    def flaky(df, state=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(df, state, **kw)

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


def test_history_rows_and_snapshot_drop_trailing_zero_volume_candle():
    """KIS 일봉은 개장 전에 오늘 봉을 거래량 0 스텁으로 돌려준다. 그 행이 피처 스토어에 들어가면
    거래대금 0 이라 scan 이 전 종목을 거른다(실기 10-20) → 마지막 봉이 거래량 0 이면 버린다."""
    from helpers_sources import make_candles

    from trader.broker.models import Candle

    candles = make_candles(30)
    from datetime import timedelta

    last = candles[-1]
    stub = Candle(
        date=last.date + timedelta(days=1),
        open=last.close,
        high=last.close,
        low=last.close,
        close=last.close,
        volume=0,
    )
    rows = features.history_rows(
        ticker="005930", market="KR", candles=[*candles, stub], ratios_by_year=None
    )
    assert rows["date"].max() == candles[-1].date and (rows["volume"] > 0).all()
    snap = features.snapshot_row(
        ticker="005930",
        market="KR",
        candles=[*candles, stub],
        flows=None,
        ratios=None,
        news_count=None,
        community_count=None,
    )
    assert snap["date"] == candles[-1].date


def test_history_rows_all_zero_volume_raises_clear_error():
    from datetime import timedelta

    from trader.broker.models import Candle

    stubs = [
        Candle(date=date(2026, 1, 1) + timedelta(days=i), open=1, high=1, low=1, close=1, volume=0)
        for i in range(3)
    ]
    with pytest.raises(ValueError, match="완성된 일봉"):
        features.history_rows(ticker="A", market="KR", candles=stubs, ratios_by_year=None)


def test_snapshot_skips_ticker_whose_today_bar_is_incomplete(project):
    """개장 전 스냅샷: 오늘 봉이 거래량 0 스텁이면 그 종목은 건너뛴다 (전날 날짜로 다시 써서 어제
    행을 오늘 수집 건수로 덮어쓰면 안 된다 — 과거 파일 불변·룩어헤드)."""
    from datetime import timedelta

    from helpers_sources import T0

    from trader.broker.models import Candle

    class StubMarket(FakeMarket):
        def get_daily_kr(self, ticker, days=250):
            c = super().get_daily_kr(ticker, days)
            if ticker == "000660":
                last = c[-1]
                stub = Candle(
                    date=last.date + timedelta(days=1),
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=0,
                )
                c = [*c, stub]
            return c

    seen = []
    n = features.snapshot(
        market_client=StubMarket(),
        dart=None,
        universe=Universe(kr=["005930", "000660"]),
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
        progress=seen.append,
    )
    assert n == 1 and any("000660" in s and "미완성" in s for s in seen)


def test_backfill_fills_us_ratios_from_edgar(project):
    universe = Universe(kr=["005930"], us=[UsSymbol(symbol="AAPL", exchange="NAS")])
    edgar = FakeEdgar()
    n = features.backfill(
        market_client=FakeMarket(candles=40),
        dart=FakeDart(),
        edgar=edgar,
        universe=universe,
        markets=["KR", "US"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 80
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    aapl = got[got["ticker"] == "AAPL"]
    assert aapl["roe"].notna().all() and float(aapl["roe"].iloc[0]) == pytest.approx(40 / 400)
    assert float(got[got["ticker"] == "005930"]["roe"].iloc[0]) == pytest.approx(40 / 400)
    assert edgar.calls == 1


def test_backfill_continues_when_edgar_fails(project):
    universe = Universe(kr=[], us=[UsSymbol(symbol="AAPL", exchange="NAS")])
    n = features.backfill(
        market_client=FakeMarket(candles=40),
        dart=None,
        edgar=FakeEdgar(fail=True),
        universe=universe,
        markets=["US"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 40  # 가격 지표 행은 저장
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    assert got["roe"].isna().all()


def test_snapshot_fills_us_ratios_from_edgar(project):
    from datetime import UTC, datetime

    universe = Universe(kr=[], us=[UsSymbol(symbol="AAPL", exchange="NAS")])
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    market = FakeMarket(candles=300, now=lambda: now)
    n = features.snapshot(
        market_client=market,
        dart=None,
        edgar=FakeEdgar(),
        universe=universe,
        markets=["US"],
        now=lambda: now,
        progress=lambda s: None,
    )
    assert n == 1
    got = pd.concat(pd.read_parquet(f) for f in (project / "state" / "features").glob("*.parquet"))
    assert float(got["roe"].iloc[0]) == pytest.approx(40 / 400)


def test_backfill_derives_once_per_date_file_not_per_chunk(project, monkeypatch):
    """묶음 쓰기마다 파생 열을 다시 계산하면 백필이 날짜 × 묶음 수만큼 느려진다 → 묶음 쓰기는 파생
    없이, 마지막에 건드린 날짜 파일만 1회 계산한다 (리뷰 #5)."""
    calls: list[int] = []
    real = features.add_sector_relative

    def counting(df, sectors):
        calls.append(len(df))
        return real(df, sectors)

    monkeypatch.setattr(features, "add_sector_relative", counting)
    monkeypatch.setattr(features, "BACKFILL_CHUNK", 1)
    universe = Universe(kr=["005930", "000660"], us=[])
    n = features.backfill(
        market_client=FakeMarket(candles=40),
        dart=FakeDart(),
        universe=universe,
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert n == 80
    assert len(calls) == 40  # 날짜 파일 수 (묶음 2개 × 40 = 80 이 아님)
    files = sorted((project / "state" / "features").glob("*.parquet"))
    assert len(files) == 40
    got = pd.read_parquet(files[-1])
    assert "roe_vs_sector" in got.columns and len(got) == 2


def test_backfill_rederives_everything_when_final_flush_fails(project, monkeypatch):
    """마지막 묶음 쓰기가 중간에 실패하면 그 전에 쓴 날짜 파일이 touched 에 안 들어가 파생 열이
    빈 채 남는다 → 그때는 전체 rederive (리뷰 #10)."""
    real = features.write_rows
    state = {"n": 0}

    def flaky(df, st=None, **kw):
        state["n"] += 1
        if state["n"] >= 2:  # 두 번째 묶음부터(재시도 포함) 절반 쓰고 실패 → 마지막 flush 도 실패
            real(df.iloc[: len(df) // 2], st, **kw)
            raise OSError("disk")
        return real(df, st, **kw)

    monkeypatch.setattr(features, "write_rows", flaky)
    monkeypatch.setattr(features, "BACKFILL_CHUNK", 1)
    seen: list = []
    real_rederive = features.rederive

    def spy(*a, **kw):
        seen.append(kw.get("paths"))
        return real_rederive(*a, **kw)

    monkeypatch.setattr(features, "rederive", spy)
    features.backfill(
        market_client=FakeMarket(candles=10),
        dart=None,
        universe=Universe(kr=["005930", "000660"]),
        markets=["KR"],
        years=1,
        progress=lambda s: None,
    )
    assert seen and seen[-1] is None  # 마지막 호출은 전체(paths=None)


def test_broken_parquet_file_is_skipped_not_fatal(project):
    """쓰기 중 종료로 깨진 날짜 파일 하나가 latest_rows·write_rows·load_all 을 죽이지 않는다."""
    from trader.ml import dataset

    candles = make_candles(3)
    df = features.history_rows(ticker="005930", market="KR", candles=candles, ratios_by_year=None)
    features.write_rows(df)
    fdir = project / "state" / "features"
    broken = sorted(fdir.glob("*.parquet"))[0]
    broken.write_bytes(b"not a parquet file")
    latest = features.latest_rows(None, "KR")
    assert list(latest["ticker"]) == ["005930"]
    assert len(dataset.load_all(None, market="KR")) == 2
    features.write_rows(df)  # 깨진 파일은 새 행으로 덮인다
    assert len(pd.read_parquet(broken)) == 1
    assert not list(fdir.glob("*.tmp"))


def test_write_rows_takes_store_lock(project, monkeypatch):
    seen = []
    from trader.ml import features as feat_mod

    orig = feat_mod.locked

    def spy(path, **kw):
        seen.append(path.name)
        return orig(path, **kw)

    monkeypatch.setattr(feat_mod, "locked", spy)
    candles = make_candles(1)
    df = features.history_rows(ticker="005930", market="KR", candles=candles, ratios_by_year=None)
    features.write_rows(df)
    features.rederive()
    assert seen == ["features.lock", "features.lock"]


def test_snapshot_tickers_override_universe(project):
    """Phase 21: `tickers=` 는 유니버스 대신 그 종목만(유니버스 밖 발굴 종목의 당일 스냅샷)."""
    from helpers_sources import T0

    n = features.snapshot(
        market_client=FakeMarket(),
        dart=FakeDart(),
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
        tickers=[("247540", None)],
    )
    assert n == 1
    got = pd.read_parquet(next((project / "state" / "features").glob("*.parquet")))
    assert list(got["ticker"]) == ["247540"]


def test_snapshot_extra_targets_are_added_to_universe(project):
    """전체 검토 2차 2-L1: `extra_targets=[(코드, 시장, 거래소)]` 는 유니버스에 **더한다**(보유·
    미만기 창의 유니버스 밖 종목도 매일 봉이 쌓이게). 유니버스와 겹치면 한 번만."""
    from helpers_sources import T0

    n = features.snapshot(
        market_client=FakeMarket(),
        dart=FakeDart(),
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
        extra_targets=[("247540", "KR", None), ("005930", "KR", None)],
    )
    assert n == 2
    got = pd.read_parquet(next((project / "state" / "features").glob("*.parquet")))
    assert sorted(got["ticker"]) == ["005930", "247540"]


def test_rederive_reports_progress_outside_store_lock(project, monkeypatch):
    """전체 검토 P9: 진행 콜백(데몬은 heavy 락 양보)이 파일 락을 쥔 채 불리지 않는다."""
    import contextlib

    features.write_rows(_rows("2026-10-01", ["005930"]))
    features.write_rows(_rows("2026-10-02", ["005930"]))
    held = {"v": False}
    real = features._store_lock

    @contextlib.contextmanager
    def spy(state):
        with real(state):
            held["v"] = True
            try:
                yield
            finally:
                held["v"] = False

    monkeypatch.setattr(features, "_store_lock", spy)
    calls: list[bool] = []
    assert features.rederive(progress=lambda s: calls.append(held["v"])) == 2
    assert calls == [False, False]


def test_write_rows_does_not_overwrite_when_existing_file_unreadable(project, monkeypatch):
    """전체 검토 P11: 날짜 파일을 일시적으로 못 읽으면(OSError) 새 행만으로 덮어쓰지 않는다."""
    features.write_rows(_rows("2026-10-01", ["A", "B"]))
    real = pd.read_parquet

    def locked_read(path, **kw):
        raise PermissionError("다른 프로세스가 사용 중")

    monkeypatch.setattr(features.pd, "read_parquet", locked_read)
    with pytest.raises(PermissionError):
        features.write_rows(_rows("2026-10-01", ["C"]))
    assert features.read_part(features._features_dir(None) / "2026-10-01.parquet") is None  # 비엄격
    monkeypatch.setattr(features.pd, "read_parquet", real)
    got = features.latest_rows(None, "KR")
    assert sorted(got["ticker"]) == ["A", "B"]


def test_atomic_replace_retries_permission_error(tmp_path, monkeypatch):
    from trader.platform import fsutil

    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("x", encoding="utf-8")
    real = fsutil.os.replace
    n = {"k": 0}

    def flaky(a, b):
        n["k"] += 1
        if n["k"] < 3:
            raise PermissionError("공유 위반")
        real(a, b)

    monkeypatch.setattr(fsutil.os, "replace", flaky)
    fsutil.atomic_replace(src, dst, delay_s=0)
    assert dst.read_text(encoding="utf-8") == "x" and n["k"] == 3
    src.write_text("y", encoding="utf-8")
    monkeypatch.setattr(fsutil.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        fsutil.atomic_replace(src, dst, attempts=2, delay_s=0)


# --- 수정주가 소급 변경 감지(Stock D-034·D-064·D-070) ------------------------------------


def test_snapshot_detects_adjusted_history_and_rebackfills(project):
    """분할·병합이 나면 KIS 는 과거 일봉 전체를 새 기준으로 다시 준다. 저장 종가와 0.5% 넘게
    다르면 그 종목만 재백필해 피처 스토어에 가격 절벽이 남지 않게 한다."""
    from helpers_sources import T0

    from trader.ml import dataset

    uni = Universe(kr=["005930", "000660"])
    features.backfill(market_client=FakeMarket(), dart=FakeDart(), universe=uni, markets=["KR"])

    class Split(FakeMarket):
        def get_daily_kr(self, ticker, days=100):
            got = super().get_daily_kr(ticker, days=days)
            if ticker != "005930":
                return got
            # 액면분할 1:2 — 마지막 봉만 그대로, 과거는 절반
            return [c.model_copy(update={"close": c.close / 2}) for c in got[:-1]] + got[-1:]

    seen = []
    features.snapshot(
        market_client=Split(),
        dart=FakeDart(),
        universe=uni,
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
        on_adjusted=lambda market, tickers: seen.append((market, tickers)),
    )
    assert seen == [("KR", ["005930"])]
    df = dataset.load_all(market="KR")
    old = df[(df.ticker == "005930")].sort_values("date").iloc[0]
    first = FakeMarket().get_daily_kr("005930", days=1000)[0]
    assert float(old["close"]) == pytest.approx(float(first.close) / 2)


def test_rebackfill_keeps_observed_columns_and_ignores_single_bar(project):
    """재백필은 수급·건수 같은 관측 열을 지우지 않고, 한두 봉 차이(장중 스냅샷 종가)는 수정주가로
    보지 않는다."""
    from helpers_sources import T0

    from trader.ml import dataset

    uni = Universe(kr=["005930"])
    features.backfill(market_client=FakeMarket(), dart=FakeDart(), universe=uni, markets=["KR"])
    df = dataset.load_all(market="KR")
    last = df.sort_values("date").iloc[-2]
    row = df[(df.date == last["date"]) & (df.ticker == "005930")].copy()
    row["flow_foreign_20d"] = 1234.0
    features.write_rows(row.assign(date=row["date"].dt.date)[features.FEATURE_COLUMNS])

    class OneBar(FakeMarket):
        def get_daily_kr(self, ticker, days=100):
            got = super().get_daily_kr(ticker, days=days)
            got[-2] = got[-2].model_copy(update={"close": got[-2].close * 2})
            return got

    seen = []
    features.snapshot(
        market_client=OneBar(),
        dart=FakeDart(),
        universe=uni,
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
        on_adjusted=lambda m, t: seen.append(t),
    )
    assert seen == []
    kept = features._keep_observed(
        features.history_rows(
            ticker="005930",
            market="KR",
            candles=FakeMarket().get_daily_kr("005930", days=1000),
            ratios_by_year={},
        ),
        None,
        "KR",
        "005930",
    )
    hit = kept[kept.date == last["date"].date()]
    assert float(hit["flow_foreign_20d"].iloc[0]) == 1234.0
