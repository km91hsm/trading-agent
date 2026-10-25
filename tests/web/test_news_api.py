"""/api/news: 시장 뉴스 저장소 조회 (토큰 없음, 읽기 전용)."""

from datetime import timedelta

from helpers_web import NOW

from trader.sources import news_store


def test_api_news_lists_recent_items_with_tags(client, conn):
    news_store.insert_items(
        conn,
        [
            {
                "title": "삼성전자 실적",
                "url": "https://a/1",
                "published_at": (NOW - timedelta(hours=1)).isoformat(),
                "received_at": (NOW - timedelta(hours=1)).isoformat(),
                "summary": "s",
            },
            {
                "title": "미래",
                "url": "https://a/2",
                "published_at": (NOW + timedelta(hours=1)).isoformat(),
                "received_at": (NOW + timedelta(hours=1)).isoformat(),
            },
        ],
        market="KR",
        source="google_rss",
        query="증시",
        lang="ko",
        names={"005930": "삼성전자"},
    )
    r = client.get("/api/news?market=KR")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and [i["title"] for i in body["items"]] == ["삼성전자 실적"]
    assert body["items"][0]["tickers"] == ["005930"] and "url_hash" not in body["items"][0]
    assert client.get("/api/news?market=KR&ticker=000660").json()["items"] == []
    assert client.get("/api/news?market=XX").status_code == 400
    assert client.get("/api/news?market=US").json()["items"] == []
