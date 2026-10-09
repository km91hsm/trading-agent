# ruff: noqa: E501
from datetime import UTC, datetime

import httpx

from trader.sources.telegram_channel import TelegramChannelSource

T0 = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)
HTML = """<html><body>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/101">
 <div class="tgme_widget_message_text">삼성전자 3분기 잠정실적 발표 예정</div>
 <a class="tgme_widget_message_date" href="https://t.me/chan/101"><time datetime="2026-10-08T23:00:00+00:00"></time></a></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/102">
 <div class="tgme_widget_message_text">금리 인하 기대</div>
 <a class="tgme_widget_message_date" href="https://t.me/chan/102"><time datetime="2026-10-09T01:00:00+00:00"></time></a></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/103">
 <div class="tgme_widget_message_text">삼성전자 미래 전망</div>
 <a class="tgme_widget_message_date" href="https://t.me/chan/103"><time datetime="2026-10-09T07:00:00+00:00"></time></a></div></div>
</body></html>"""


def test_telegram_preview_parses_and_filters(respx_mock):
    respx_mock.get("https://t.me/s/chan").mock(
        return_value=httpx.Response(200, content=HTML.encode("utf-8"))
    )
    src = TelegramChannelSource(["chan"], http=httpx.Client(), max_posts=30, now=lambda: T0)
    r = src.fetch(keywords=("삼성전자",), asof=T0)
    posts = r["sources"]["telegram/chan"]
    assert [p["url"] for p in posts] == [
        "https://t.me/chan/101"
    ]  # 102 키워드 불일치, 103 asof 이후
    assert (
        posts[0]["title"].startswith("삼성전자 3분기")
        and posts[0]["posted_at"] == "2026-10-08T23:00:00+00:00"
    )
    assert posts[0]["source"] == "telegram/chan" and r["errors"] == {}


def test_telegram_unexpected_html_gives_empty_and_error_isolated(respx_mock):
    respx_mock.get("https://t.me/s/chan").mock(
        return_value=httpx.Response(200, content=b"<html></html>")
    )
    respx_mock.get("https://t.me/s/dead").mock(return_value=httpx.Response(500))
    src = TelegramChannelSource(["chan", "dead"], http=httpx.Client(), now=lambda: T0)
    r = src.fetch(keywords=("삼성전자",), asof=T0)
    assert r["sources"]["telegram/chan"] == [] and "telegram/dead" in r["errors"]
