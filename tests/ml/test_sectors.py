"""동종업 중앙값 상대 비율 (`*_vs_sector`): 같은 날·같은 시장·같은 섹터의 중앙값 대비 차이."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from trader.config import SectorMap
from trader.ml import features

SECTORS = SectorMap(
    sectors=["반도체", "금융", "기타"],
    kr={"A": "반도체", "B": "반도체", "C": "반도체", "D": "금융", "E": "금융"},
    us={"AAPL": "반도체", "NVDA": "반도체", "AMD": "반도체"},
)


def _rows(day: date, market: str, roes: dict[str, float | None]) -> pd.DataFrame:
    n = len(roes)
    return pd.DataFrame(
        {
            "date": [day] * n,
            "ticker": list(roes),
            "market": [market] * n,
            "close": [100.0] * n,
            "volume": [1000] * n,
            "roe": list(roes.values()),
            "op_margin": [0.1] * n,
            "debt_ratio": [1.0] * n,
        }
    )


def test_sector_relative_is_value_minus_group_median():
    df = _rows(date(2026, 1, 5), "KR", {"A": 0.1, "B": 0.2, "C": 0.3})
    out = features.add_sector_relative(df, SECTORS)
    assert out["roe_vs_sector"].tolist() == [-0.1, 0.0, 0.1] or np.allclose(
        out["roe_vs_sector"].astype(float), [-0.1, 0.0, 0.1]
    )
    assert np.allclose(out["op_margin_vs_sector"].astype(float), [0.0, 0.0, 0.0])
    assert set(features.SECTOR_COLUMNS) <= set(out.columns)


def test_sector_relative_nan_when_fewer_than_three():
    """금융 2종목 → NaN (0 이 아니다), 값이 비어 있는 구성원은 세지 않는다."""
    df = _rows(date(2026, 1, 5), "KR", {"A": 0.1, "B": None, "C": 0.3, "D": 0.1, "E": 0.2})
    out = features.add_sector_relative(df, SECTORS).set_index("ticker")
    assert out.loc["D", "roe_vs_sector"] is pd.NA or pd.isna(out.loc["D", "roe_vs_sector"])
    assert pd.isna(out.loc["A", "roe_vs_sector"])  # 반도체는 값 있는 구성원이 2개
    assert pd.isna(out.loc["B", "roe_vs_sector"])


def test_sector_relative_groups_by_date_and_market_and_unknown_ticker_nan():
    kr = _rows(date(2026, 1, 5), "KR", {"A": 0.1, "B": 0.2, "C": 0.6})
    us = _rows(date(2026, 1, 5), "US", {"AAPL": 0.0, "NVDA": 0.0, "AMD": 0.3})
    other_day = _rows(date(2026, 1, 6), "KR", {"A": 1.0, "B": 1.0, "C": 1.0})
    unknown = _rows(date(2026, 1, 5), "KR", {"ZZZ": 0.5})
    out = features.add_sector_relative(pd.concat([kr, us, other_day, unknown]), SECTORS)
    out = out.set_index(["date", "market", "ticker"])["roe_vs_sector"].astype(float)
    assert out[(date(2026, 1, 5), "KR", "C")] == pytest.approx(0.4)  # KR 중앙값 0.2
    assert out[(date(2026, 1, 5), "US", "AMD")] == pytest.approx(0.3)  # US 중앙값 0.0 (KR 과 따로)
    assert out[(date(2026, 1, 6), "KR", "A")] == pytest.approx(0.0)  # 다른 날은 따로
    assert np.isnan(out[(date(2026, 1, 5), "KR", "ZZZ")])


def test_write_rows_recomputes_derived_over_whole_file_idempotently(project):
    """백필은 종목 묶음으로 나눠 쓴다 — 마지막 쓰기 뒤 파생 열은 한 번에 쓴 것과 같아야 하고,
    다른 시장 행이 보존되며, 다시 써도 값이 같다."""
    day = date(2026, 1, 5)
    full = pd.concat(
        [
            _rows(day, "KR", {"A": 0.1, "B": 0.2, "C": 0.3}),
            _rows(day, "US", {"AAPL": 0.0, "NVDA": 0.1, "AMD": 0.5}),
        ]
    )
    features.write_rows(full, sectors=SECTORS)
    once = pd.read_parquet(project / "state" / "features" / "2026-01-05.parquet")
    once = once.set_index("ticker")["roe_vs_sector"].astype(float).sort_index()

    (project / "state" / "features" / "2026-01-05.parquet").unlink()
    features.write_rows(full[full["ticker"].isin(["A", "AAPL", "NVDA", "AMD"])], sectors=SECTORS)
    features.write_rows(full[full["ticker"].isin(["B", "C"])], sectors=SECTORS)
    features.write_rows(full[full["ticker"].isin(["B", "C"])], sectors=SECTORS)  # 멱등
    chunked = pd.read_parquet(project / "state" / "features" / "2026-01-05.parquet")
    assert sorted(chunked["ticker"]) == ["A", "AAPL", "AMD", "B", "C", "NVDA"]
    chunked = chunked.set_index("ticker")["roe_vs_sector"].astype(float).sort_index()
    pd.testing.assert_series_equal(once, chunked)
    assert chunked["C"] == pytest.approx(0.1) and chunked["AMD"] == pytest.approx(0.4)


def test_rederive_rewrites_all_files(project):
    """예전 파일(파생 열 없음)을 KIS 호출 없이 다시 계산한다."""
    d = project / "state" / "features"
    d.mkdir(parents=True)
    for day in (date(2026, 1, 5), date(2026, 1, 6)):
        _rows(day, "KR", {"A": 0.1, "B": 0.2, "C": 0.3}).to_parquet(d / f"{day}.parquet")
    n = features.rederive(sectors=SECTORS, progress=lambda s: None)
    assert n == 2
    raw = pd.read_parquet(d / "2026-01-06.parquet")
    assert list(raw.columns) == list(features.FEATURE_COLUMNS)
    got = raw.set_index("ticker")
    assert float(got.loc["C", "roe_vs_sector"]) == pytest.approx(0.1)


def test_sector_medians_from_latest_rows(project):
    features.write_rows(
        _rows(date(2026, 1, 5), "KR", {"A": 0.1, "B": 0.2, "C": 0.6, "D": 0.5}), sectors=SECTORS
    )
    m = features.sector_medians(None, "KR", "반도체", tickers=["A", "B", "C"])
    assert m["n"] == 3 and m["median"]["roe"] == 0.2 and m["median"]["op_margin"] == 0.1
    assert features.sector_medians(None, "KR", "금융", tickers=["D", "E"])["n"] == 1
