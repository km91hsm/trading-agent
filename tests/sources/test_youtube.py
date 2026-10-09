# ruff: noqa: E501
from datetime import UTC, datetime

import httpx

from trader.sources.youtube import YouTubeSource

T0 = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)
RSS = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>채널A</title>
 <entry><yt:videoId>v1</yt:videoId><title>삼성전자 실적 분석</title><published>2026-10-08T10:00:00+00:00</published>
  <link rel="alternate" href="https://www.youtube.com/watch?v=v1"/><author><name>채널A</name></author>
  <media:group><media:description>HBM 이야기</media:description></media:group></entry>
 <entry><yt:videoId>v2</yt:videoId><title>오늘의 거시 뉴스</title><published>2026-10-08T09:00:00+00:00</published>
  <link rel="alternate" href="https://www.youtube.com/watch?v=v2"/><media:group><media:description>금리</media:description></media:group></entry>
 <entry><yt:videoId>v3</yt:videoId><title>삼성전자 미래</title><published>2026-10-09T07:00:00+00:00</published>
  <link rel="alternate" href="https://www.youtube.com/watch?v=v3"/><media:group><media:description>x</media:description></media:group></entry>
</feed>"""


def test_youtube_rss_and_transcript_filtered_by_keyword_and_asof(respx_mock):
    respx_mock.get(url__startswith="https://www.youtube.com/feeds/videos.xml").mock(
        return_value=httpx.Response(200, content=RSS.encode("utf-8"))
    )
    calls = []

    def transcript(video_id: str) -> str:
        calls.append(video_id)
        return "자막 " * 2000 if video_id == "v1" else "짧은 자막"

    src = YouTubeSource(
        ["UCabc"], http=httpx.Client(), max_videos=3, transcript_fn=transcript, now=lambda: T0
    )
    r = src.fetch(keywords=("삼성전자", "005930"), asof=T0)
    items = r["sources"]["youtube/UCabc"]
    assert [i["video_id"] for i in items] == ["v1"]  # v2 키워드 불일치, v3 asof 이후
    assert items[0]["title"] == "삼성전자 실적 분석" and len(items[0]["transcript_excerpt"]) <= 2000
    assert (
        items[0]["posted_at"] == "2026-10-08T10:00:00+00:00"
        and items[0]["source"] == "youtube/UCabc"
    )
    assert calls == ["v1"] and r["errors"] == {}


def test_youtube_transcript_failure_recorded_per_video(respx_mock):
    respx_mock.get(url__startswith="https://www.youtube.com/feeds/videos.xml").mock(
        return_value=httpx.Response(200, content=RSS.encode("utf-8"))
    )

    def boom(video_id):
        raise RuntimeError("no transcript")

    src = YouTubeSource(["UCabc"], http=httpx.Client(), transcript_fn=boom, now=lambda: T0)
    r = src.fetch(keywords=("삼성전자",), asof=T0)
    assert r["sources"]["youtube/UCabc"][0]["transcript_excerpt"] == ""
    assert "v1" in r["errors"]["youtube/UCabc"]


def test_youtube_channel_http_error_isolated(respx_mock):
    respx_mock.get(url__startswith="https://www.youtube.com/feeds/videos.xml").mock(
        return_value=httpx.Response(404)
    )
    src = YouTubeSource(["UCbad"], http=httpx.Client(), transcript_fn=lambda v: "", now=lambda: T0)
    r = src.fetch(keywords=("x",), asof=T0)
    assert r["sources"] == {} and "youtube/UCbad" in r["errors"]
