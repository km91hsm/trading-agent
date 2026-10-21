"""features: 피처 스토어 조회 API (토큰 없음, 읽기 전용, 입력 검증)."""

from datetime import date

import pandas as pd
import yaml

from trader.config import SectorMap
from trader.ml import features

SECTORS = SectorMap(sectors=["반도체", "기타"], kr={"005930": "반도체", "000660": "반도체"})


def _rows(day: date, tickers: dict[str, float | None]) -> pd.DataFrame:
    n = len(tickers)
    return pd.DataFrame(
        {
            "date": [day] * n,
            "ticker": list(tickers),
            "market": ["KR"] * n,
            "close": [100.0 + i for i in range(n)],
            "volume": [1000] * n,
            "roe": list(tickers.values()),
            "roc_10": [0.01] * n,
        }
    )


def _seed(project):
    (project / "config" / "sectors.yaml").write_text(
        yaml.safe_dump(SECTORS.model_dump(), allow_unicode=True), encoding="utf-8"
    )
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930", "000660"]}), encoding="utf-8"
    )
    for day in (date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)):
        features.write_rows(_rows(day, {"005930": 0.1, "000660": None}), sectors=SECTORS)


def test_latest_returns_rows_with_sector(client, project):
    _seed(project)
    r = client.get("/api/features/latest?market=KR")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["market"] == "KR" and body["asof"] == "2026-01-07"
    assert "roe_vs_sector" in body["columns"] and "sector" in body["columns"]
    rows = {row["ticker"]: row for row in body["rows"]}
    assert set(rows) == {"005930", "000660"}
    assert rows["005930"]["sector"] == "반도체" and rows["005930"]["date"] == "2026-01-07"
    assert rows["000660"]["roe"] is None  # NaN → null (JSON 에 NaN 금지)


def test_latest_rejects_unknown_market_and_empty_store(client, project):
    assert client.get("/api/features/latest?market=JP").status_code == 400
    r = client.get("/api/features/latest?market=KR")
    assert r.status_code == 200 and r.json()["rows"] == [] and r.json()["asof"] is None


def test_history_filters_ticker_and_days(client, project):
    _seed(project)
    r = client.get("/api/features/history?market=KR&ticker=005930&days=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticker"] == "005930" and [x["date"] for x in body["rows"]] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert all(x["ticker"] == "005930" for x in body["rows"])
    r = client.get("/api/features/history?market=KR&ticker=000660&days=5")
    assert r.json()["rows"][0]["roe"] is None


def test_history_rejects_bad_ticker_and_days(client, project):
    assert client.get("/api/features/history?market=KR&ticker=../x&days=10").status_code == 400
    assert client.get("/api/features/history?market=KR&ticker=005930&days=1").status_code == 422
    assert client.get("/api/features/history?market=KR&ticker=005930&days=5000").status_code == 422
    assert client.get("/api/features/history?market=XX&ticker=005930").status_code == 400


def test_history_unknown_ticker_is_404_not_full_scan(client, project):
    """유니버스에 없는 종목은 파일을 다 읽지 않고 404 (토큰 없는 API 비용 방어, 리뷰 #10)."""
    _seed(project)
    assert client.get("/api/features/history?market=KR&ticker=999999&days=10").status_code == 404
    assert client.get("/api/features/history?market=KR&ticker=005930&days=10").status_code == 200


def test_latest_tolerates_broken_sectors_yaml(client, project):
    _seed(project)
    (project / "config" / "sectors.yaml").write_text(
        "sectors: [반도체]\nkr: {'005930': 금융}\n", encoding="utf-8"
    )
    r = client.get("/api/features/latest?market=KR")
    assert r.status_code == 200 and r.json()["rows"][0]["sector"] is None
