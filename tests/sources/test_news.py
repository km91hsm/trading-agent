import httpx
import pytest
from helpers_sources import T0, load, load_bytes

from trader.config import NewsCfg, Secrets
from trader.sources.news import GoogleNewsRss, NaverNews, collect_news

GOOGLE = "https://news.google.com/rss/search"
NAVER = "https://naverapihub.apigw.ntruss.com/search/v1/news"


def test_google_rss_parsed(respx_mock):
    respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    items = GoogleNewsRss(httpx.Client(), now=lambda: T0).search(
        "삼성전자", lang="ko", max_items=10
    )
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "삼성전자, 3분기 잠정실적 발표 - 연합뉴스"
    assert first["source"] == "연합뉴스" and first["url"].endswith("/A1")
    assert first["published_at"] == "2026-10-08T23:10:00+00:00"
    assert first["received_at"] == T0.isoformat()
    assert "<a" not in first["summary"]


def test_naver_requires_keys_else_error(respx_mock):
    with pytest.raises(ValueError, match="키"):
        NaverNews(None, None, httpx.Client())


def test_naver_parsed_with_header_keys(respx_mock):
    route = respx_mock.get(url__startswith=NAVER).mock(
        return_value=httpx.Response(200, json=load("news_naver.json"))
    )
    items = NaverNews("CID-SECRET", "CSEC-SECRET", httpx.Client(), now=lambda: T0).search(
        "삼성전자", max_items=10
    )
    assert items[0]["title"] == "삼성전자 반도체 투자 확대"
    assert items[0]["published_at"] == "2026-10-09T01:00:00+00:00"
    assert items[0]["url"] == "https://www.example.com/n1"
    req = route.calls[0].request
    assert req.headers["X-NCP-APIGW-API-KEY-ID"] == "CID-SECRET"
    assert "CID-SECRET" not in str(req.url)


def test_lookahead_items_filtered(respx_mock):
    respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    respx_mock.get(url__startswith=NAVER).mock(
        return_value=httpx.Response(200, json=load("news_naver.json"))
    )
    cfg = NewsCfg(enabled=True, google_rss=True, naver=True, max_items=20, macro_items=5)
    secrets = Secrets(NAVER_CLIENT_ID="a", NAVER_CLIENT_SECRET="b")
    out = collect_news(
        cfg,
        secrets,
        ticker="005930",
        name="삼성전자",
        market="KR",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    urls = [i["url"] for i in out["stock"]]
    # 09:00Z(미래)·10:00 KST(=01:00Z, asof 06:00Z 이전이라 포함) 구분, 중복 URL 제거
    assert "https://news.google.com/rss/articles/A2" not in urls
    assert urls.count("https://news.google.com/rss/articles/A1") == 1
    assert "https://www.example.com/n1" in urls and "https://www.example.com/n2" in urls
    assert all(i["published_at"] <= T0.isoformat() for i in out["stock"])
    assert len(out["macro"]) <= 5
    assert out["stock"] == sorted(out["stock"], key=lambda i: i["published_at"], reverse=True)


def test_dedupe_by_url_and_naver_skipped_without_keys(respx_mock):
    respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    cfg = NewsCfg(enabled=True, google_rss=True, naver=True, max_items=20)
    out = collect_news(
        cfg,
        Secrets(),
        ticker="005930",
        name="삼성전자",
        market="KR",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    assert len({i["url"] for i in out["stock"]}) == len(out["stock"])
    assert out["sources"]["naver"] == "no api key"


def test_us_uses_english_query(respx_mock):
    route = respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    cfg = NewsCfg(enabled=True, google_rss=True, naver=False)
    collect_news(
        cfg,
        Secrets(),
        ticker="AAPL",
        name="Apple",
        market="US",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    first = str(route.calls[0].request.url)
    assert "hl=en-US" in first and "AAPL" in first


def test_google_failure_is_recorded_not_raised(respx_mock):
    respx_mock.get(url__startswith=GOOGLE).mock(side_effect=httpx.ConnectError("x"))
    cfg = NewsCfg(enabled=True, google_rss=True, naver=False)
    out = collect_news(
        cfg,
        Secrets(),
        ticker="005930",
        name="삼성전자",
        market="KR",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    assert out["stock"] == [] and out["sources"]["google_rss"].startswith("ConnectError")


def test_collect_news_closes_self_made_client(respx_mock, monkeypatch):
    respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    made: list[httpx.Client] = []
    real = httpx.Client

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(httpx, "Client", Spy)
    cfg = NewsCfg(enabled=True, google_rss=True, naver=False)
    collect_news(
        cfg, Secrets(), ticker="005930", name="삼성전자", market="KR", asof=T0, now=lambda: T0
    )
    assert len(made) == 1 and made[0].is_closed


def test_naver_uses_api_hub_endpoint_and_headers(respx_mock):
    """네이버 검색 API 는 NAVER API HUB(네이버 클라우드)로 이관됐다 — 새 호스트·경로·인증 헤더.
    개발자센터 키/호스트를 섞어 쓰면 401 (실기 2026-10-20: 두 저장소 키 모두 401 의 원인)."""
    from trader.sources.news import NaverNews

    route = respx_mock.get("https://naverapihub.apigw.ntruss.com/search/v1/news").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "title": "삼성전자 <b>실적</b>",
                        "originallink": "https://ex.com/a",
                        "link": "https://n.news.naver.com/a",
                        "description": "요약",
                        "pubDate": "Thu, 08 Oct 2026 10:00:00 +0900",
                    }
                ]
            },
        )
    )
    client = NaverNews("HUB-ID", "HUB-SECRET-40CHARS", httpx.Client())
    items = client.search("삼성전자", max_items=5)
    assert len(items) == 1 and items[0]["url"] == "https://ex.com/a"
    req = route.calls[0].request
    assert req.headers["X-NCP-APIGW-API-KEY-ID"] == "HUB-ID"
    assert req.headers["X-NCP-APIGW-API-KEY"] == "HUB-SECRET-40CHARS"
    assert "X-Naver-Client-Id" not in req.headers
    assert req.url.params["display"] == "5" and req.url.params["sort"] == "date"
