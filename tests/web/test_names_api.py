"""GET /api/names: 원장 ticker_names → 캐시 JSON 폴백, 모르는 종목은 키 없음, 토큰 없음."""

import json

from trader.ledger import repo


def test_names_from_ledger_and_cache(client, conn, project):
    repo.upsert_ticker_name(conn, "KR", "005930", "삼성전자", "balance")
    cache = project / "state" / "cache"
    cache.mkdir(parents=True)
    (cache / "edgar_tickers.json").write_text(
        json.dumps(
            {
                "fetched_at": "2020-01-01T00:00:00+00:00",
                "map": {"AAPL": {"cik": "1", "name": "Apple Inc."}},
            }
        ),
        encoding="utf-8",
    )
    assert client.get("/api/names").json() == {"KR:005930": "삼성전자"}
    r = client.get("/api/names", params={"market": "US", "tickers": "AAPL,ZZZZ"})
    assert r.status_code == 200 and r.json() == {"US:AAPL": "Apple Inc."}
    # 캐시 적중은 원장에 남아 다음부터 전체 조회에도 나온다
    assert client.get("/api/names").json() == {"KR:005930": "삼성전자", "US:AAPL": "Apple Inc."}


def test_names_rejects_bad_market_and_ticker(client):
    assert client.get("/api/names", params={"market": "JP", "tickers": "A"}).status_code == 400
    assert client.get("/api/names", params={"market": "KR", "tickers": "../x"}).status_code == 400
    assert client.get("/api/names", params={"tickers": "005930"}).status_code == 400  # market 필수
