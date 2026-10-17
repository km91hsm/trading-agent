import httpx
from helpers_sources import T0, load_bytes

from trader.config import CommunityCfg
from trader.sources.community import Clien, DcInside, FmKorea, Reddit, collect_community

REDDIT = "https://www.reddit.com/r/"
CLIEN = "https://www.clien.net/service/board/cm_stock"
DC = "https://gall.dcinside.com/mgallery/board/lists/"
FM = "https://www.fmkorea.com/"


def test_reddit_rss_parsed(respx_mock):
    respx_mock.get(url__startswith=REDDIT).mock(
        return_value=httpx.Response(200, content=load_bytes("reddit_new.rss"))
    )
    posts = Reddit(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(
        "stocks", keywords=("AAPL", "Apple")
    )
    assert [p["title"] for p in posts] == ["AAPL earnings next week, thoughts?", "AAPL future post"]
    p = posts[0]
    assert p["author"] == "trader1" and p["posted_at"] == "2026-10-09T05:30:00+00:00"
    assert p["body"] == "Expecting a beat on services." and p["source"] == "reddit/r/stocks"
    assert p["url"].endswith("/aapl_earnings/") and p["received_at"] == T0.isoformat()


def test_reddit_posted_at_normalized_to_utc(respx_mock):
    """+09:00 오프셋 항목도 UTC 로 정규화돼 asof 비교가 안전하다 (리뷰 I2)."""
    respx_mock.get(url__startswith=REDDIT).mock(
        return_value=httpx.Response(200, content=load_bytes("reddit_new.rss"))
    )
    posts = Reddit(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(
        "stocks", keywords=("AAPL",)
    )
    assert posts[1]["posted_at"] == "2026-10-09T07:00:00+00:00"
    out = collect_community(
        CommunityCfg(),
        ticker="AAPL",
        name="Apple",
        market="US",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
        sleep=lambda s: None,
    )
    assert all(p["title"] != "AAPL future post" for p in out["posts"])


def test_clien_list_filtered_by_name(respx_mock):
    respx_mock.get(url__startswith=CLIEN).mock(
        return_value=httpx.Response(200, content=load_bytes("clien_board.html"))
    )
    posts = Clien(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(
        keywords=("삼성전자", "005930")
    )
    assert [p["title"] for p in posts] == ["삼성전자 실적 어떻게 보세요", "삼성전자 미래글"]
    assert posts[0]["posted_at"] == "2026-10-09T05:10:00+00:00"  # 14:10 KST
    assert posts[0]["url"].startswith("https://www.clien.net/service/board/cm_stock/1001")
    assert posts[0]["author"] == "투자자A" and posts[0]["source"] == "clien"


def test_clien_unexpected_html_gives_empty(respx_mock):
    respx_mock.get(url__startswith=CLIEN).mock(
        return_value=httpx.Response(200, content=b"<html><body><p>changed</p></body></html>")
    )
    assert Clien(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(keywords=("x",)) == []


def test_dcinside_and_fmkorea_parsed(respx_mock):
    respx_mock.get(url__startswith=DC).mock(
        return_value=httpx.Response(200, content=load_bytes("dc_board.html"))
    )
    respx_mock.get(url__startswith=FM).mock(
        return_value=httpx.Response(200, content=load_bytes("fmkorea_board.html"))
    )
    dc = DcInside(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(
        "kospi", keywords=("삼성전자",)
    )
    assert [p["title"] for p in dc] == ["삼성전자 7만 가나"]
    assert dc[0]["posted_at"] == "2026-10-09T05:05:11+00:00" and dc[0]["source"] == "dcinside/kospi"
    fm = FmKorea(httpx.Client(), now=lambda: T0, sleep=lambda s: None).fetch(keywords=("삼성전자",))
    assert [p["title"] for p in fm] == ["삼성전자 실적 발표 정리"]
    assert fm[0]["posted_at"] == "2026-10-09T05:20:00+00:00"


def test_disabled_sources_not_called_and_lookahead(respx_mock):
    clien = respx_mock.get(url__startswith=CLIEN).mock(
        return_value=httpx.Response(200, content=load_bytes("clien_board.html"))
    )
    dc = respx_mock.get(url__startswith=DC).mock(return_value=httpx.Response(200, content=b""))
    fm = respx_mock.get(url__startswith=FM).mock(return_value=httpx.Response(200, content=b""))
    cfg = CommunityCfg()  # 정책 기본값: reddit·clien on, dcinside·fmkorea off
    out = collect_community(
        cfg,
        ticker="005930",
        name="삼성전자",
        market="KR",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
        sleep=lambda s: None,
    )
    assert clien.call_count == 1 and dc.call_count == 0 and fm.call_count == 0
    titles = [p["title"] for p in out["posts"]]
    assert (
        "삼성전자 실적 어떻게 보세요" in titles and "삼성전자 미래글" not in titles
    )  # 16:30 KST > asof
    assert out["sources"]["clien"] == "ok" and "dcinside" not in out["sources"]
    assert out["sources"]["reddit"] == "not available for KR"


def test_us_uses_reddit_only(respx_mock):
    reddit = respx_mock.get(url__startswith=REDDIT).mock(
        return_value=httpx.Response(200, content=load_bytes("reddit_new.rss"))
    )
    clien = respx_mock.get(url__startswith=CLIEN).mock(
        return_value=httpx.Response(200, content=b"")
    )
    out = collect_community(
        CommunityCfg(),
        ticker="AAPL",
        name="Apple",
        market="US",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
        sleep=lambda s: None,
    )
    assert reddit.call_count >= 1 and clien.call_count == 0
    assert all(p["source"].startswith("reddit") for p in out["posts"])
    assert all(p["posted_at"] <= T0.isoformat() for p in out["posts"])


def test_source_failure_recorded(respx_mock):
    respx_mock.get(url__startswith=CLIEN).mock(side_effect=httpx.ConnectError("x"))
    out = collect_community(
        CommunityCfg(reddit=False),
        ticker="005930",
        name="삼성전자",
        market="KR",
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
        sleep=lambda s: None,
    )
    assert out["posts"] == [] and out["sources"]["clien"].startswith("ConnectError")


# --- Phase 6 보강 ------------------------------------------------------------------


def test_keyword_ascii_word_boundary():
    from trader.sources.community import _matches

    assert _matches("Is F stock a buy?", ("F",)) is True
    assert _matches("Turn OFF the lights", ("F",)) is False
    assert _matches("$AAPL earnings", ("AAPL",)) is True
    assert _matches("삼성전자우 분석", ("삼성전자",)) is True  # 한글은 부분 일치 유지


def test_reddit_oauth_when_keys_present(respx_mock):
    import httpx

    from trader.sources.community import Reddit

    token = respx_mock.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
    )
    listing = respx_mock.get(url__startswith="https://oauth.reddit.com/r/stocks/new").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "AAPL to the moon",
                                "selftext": "body",
                                "author": "u1",
                                "created_utc": 1791000000,
                                "permalink": "/r/stocks/comments/x/",
                            }
                        },
                        {
                            "data": {
                                "title": "unrelated",
                                "selftext": "",
                                "author": "u2",
                                "created_utc": 1791000000,
                                "permalink": "/r/stocks/comments/y/",
                            }
                        },
                    ]
                }
            },
        )
    )
    r = Reddit(httpx.Client(), auth=("cid", "csec", "me"), sleep=lambda s: None)
    posts = r.fetch("stocks", keywords=("AAPL",), max_posts=10)
    assert token.called and listing.called
    assert len(posts) == 1 and posts[0]["url"] == "https://www.reddit.com/r/stocks/comments/x/"
    assert posts[0]["posted_at"].endswith("+00:00")
    req = token.calls[0].request
    assert (
        req.headers["authorization"].startswith("Basic ")
        and "(by /u/me)" in req.headers["user-agent"]
    )
    assert listing.calls[0].request.headers["authorization"] == "Bearer tok"


def test_collect_community_closes_self_made_client(respx_mock, monkeypatch):
    respx_mock.get(url__startswith=REDDIT).mock(
        return_value=httpx.Response(200, content=load_bytes("reddit_new.rss"))
    )
    made: list[httpx.Client] = []
    real = httpx.Client

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(httpx, "Client", Spy)
    collect_community(
        CommunityCfg(),
        ticker="AAPL",
        name="Apple",
        market="US",
        asof=T0,
        now=lambda: T0,
        sleep=lambda s: None,
    )
    assert len(made) == 1 and made[0].is_closed
