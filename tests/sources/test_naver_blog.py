from datetime import UTC, datetime

import httpx

from trader.sources.naver_blog import NaverBlogSource

XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title><![CDATA[메르의 블로그]]></title>
<item><author>ranto28</author><category><![CDATA[경제]]></category>
<title><![CDATA[미래 글]]></title><link><![CDATA[https://blog.naver.com/ranto28/3]]></link>
<description><![CDATA[<p>아직 안 나온 글</p>]]></description>
<pubDate>Sun, 25 Oct 2026 06:13:22 +0900</pubDate></item>
<item><author>ranto28</author><category><![CDATA[경제]]></category>
<title><![CDATA[네타냐후는 계속 집권할 수 있을까?]]></title><link><![CDATA[https://blog.naver.com/ranto28/2]]></link>
<description><![CDATA[<p>본문 <b>굵게</b> &amp; 더</p>]]></description>
<pubDate>Fri, 23 Oct 2026 07:00:00 +0900</pubDate></item>
<item><title><![CDATA[유가 정리]]></title><link>https://blog.naver.com/ranto28/1</link>
<description><![CDATA[%s]]></description><pubDate>Thu, 22 Oct 2026 07:00:00 +0900</pubDate></item>
</channel></rss>""" % ("가" * 3000)

ASOF = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


def test_fetch_filters_asof_strips_tags_and_caps(respx_mock):
    respx_mock.get("https://rss.blog.naver.com/ranto28.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    r = NaverBlogSource(["ranto28"], http=httpx.Client(), max_posts=5, now=lambda: T0).fetch(
        asof=ASOF, keywords=("삼성전자",)
    )
    items = r["sources"]["naver_blog/ranto28"]
    assert [i["title"] for i in items] == ["네타냐후는 계속 집권할 수 있을까?", "유가 정리"]
    assert items[0]["body"] == "본문 굵게 & 더" and items[0]["category"] == "경제"
    assert items[0]["posted_at"] == "2026-10-22T22:00:00+00:00"
    assert items[0]["received_at"] == T0.isoformat() and items[0]["source"] == "naver_blog/ranto28"
    assert len(items[1]["body"]) == 2000 and items[1]["author"] == "ranto28"
    assert r["errors"] == {}


def test_fetch_max_posts_and_error_isolation(respx_mock):
    respx_mock.get("https://rss.blog.naver.com/ranto28.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    respx_mock.get("https://rss.blog.naver.com/down.xml").mock(return_value=httpx.Response(500))
    r = NaverBlogSource(["ranto28", "down"], http=httpx.Client(), max_posts=1).fetch(asof=ASOF)
    assert len(r["sources"]["naver_blog/ranto28"]) == 1
    assert "down" not in r["sources"] and "HTTPStatusError" in r["errors"]["naver_blog/down"]


def test_rejects_bad_id():
    try:
        NaverBlogSource(["../x"], http=httpx.Client())
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError 가 나야 한다")


def test_collect_channels_includes_naver_blog(respx_mock):
    from types import SimpleNamespace

    from trader.sources.community import collect_channels

    respx_mock.get("https://rss.blog.naver.com/ranto28.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    ch = SimpleNamespace(youtube=[], telegram=[], naver_blog=["ranto28"], naver_blog_posts=2)
    out = collect_channels(ch, keywords=("x",), asof=ASOF, http=httpx.Client())
    assert len(out["sources"]["naver_blog/ranto28"]) == 2 and out["errors"] == {}
