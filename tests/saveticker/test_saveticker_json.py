from trader.saveticker import jsonfeed
from trader.saveticker.service import build_views


def test_trends_removed():
    assert "community_trends" not in {v.stream for v in build_views("2026-10", "2026-10")}


def test_structured_body_preserves_paragraphs_without_metadata():
    assert (
        jsonfeed.body_text(
            [
                {"type": "paragraph", "children": [{"text": "hello"}, {"text": " world"}]},
                {"type": "paragraph", "children": [{"text": "next"}]},
            ]
        )
        == "hello world\nnext"
    )


def test_news_detail_not_list_teaser_and_no_user_fields():
    row = {
        "id": "n1",
        "title": "Title",
        "content": "teaser",
        "source": "Reuters",
        "author_name": "private",
        "comment_count": 3,
    }
    item = jsonfeed.news_item(row)
    assert item.body == "" and item.detail_status == "pending"
    detail = jsonfeed.news_item(
        {**row, "content": [{"type": "paragraph", "children": [{"text": "complete"}]}]}, detail=True
    )
    assert detail.body == "complete" and detail.detail_status == "ok"
    assert detail.author == "" and detail.comment_count is None


def test_calendar_excludes_adjacent_month_and_preserves_all_day():
    rows = jsonfeed.extract(
        {
            "events": [
                {"id": "a", "title": "A", "event_date": "2026-09-30T01:40:00"},
                {
                    "id": "b",
                    "title": "B",
                    "event_date": "2026-10-01T12:00:00",
                    "event_date_only": True,
                },
            ]
        },
        "calendar_2026-10",
        month="2026-10",
    )
    assert len(rows) == 1 and rows[0].scheduled_date == "2026-10-01"
    assert rows[0].scheduled_at is None


def test_rankings_nested_ticker_and_regular_prices():
    rows = jsonfeed.extract(
        {
            "rankings": [
                {
                    "rank": 1,
                    "ticker": {"symbol": "AAA", "nameKo": "가"},
                    "price": 2,
                    "changePercent": 3,
                    "regular": {"price": 1, "changePercent": -1},
                }
            ]
        },
        "community_top5",
        regular=True,
    )
    assert rows[0].tickers == ["AAA"] and rows[0].price == "1"


def test_response_route_requires_host_and_exact_paths():
    assert jsonfeed.route("https://www.saveticker.com/api/news/list?page=1") == "news"
    for url in [
        "https://evil.com/api/news/list",
        "https://www.saveticker.com/api/community/list",
        "https://www.saveticker.com/api/stocks/api/v1/community/feed/messages",
    ]:
        assert jsonfeed.route(url) is None


def test_regular_popular_response_uses_already_selected_prices():
    rows = jsonfeed.extract(
        {"basis": "regular", "items": [{"symbol": "AAA", "price": 2, "changePercent": 1}]},
        "market_gain_regular",
        regular=True,
    )
    assert rows[0].price == "2"


def test_news_classification_and_tickers_match_existing_identity():
    item = jsonfeed.news_item(
        {"id": "n", "title": "T", "source": "reuters", "tickers": [{"symbol": "AAA"}]}
    )
    assert item.publisher == "로이터" and item.source_group == "로이터"
    assert item.tickers == ["AAA"]


def test_browser_uses_only_current_json_and_rejects_denied_response():
    import pytest

    from trader.saveticker.browser import AccessDenied, Browser
    from trader.saveticker.service import View

    class Page:
        def on(self, *_):
            pass

    class Response:
        url = "https://www.saveticker.com/api/stocks/api/v1/tickers/popular"
        status = 200

        def json(self):
            return {"items": [{"symbol": "AAA", "price": 2}]}

    adapter = Browser(Page(), ("unused", "unused"))
    adapter._json_response(Response())
    assert adapter._json_rows(View("market_gain_live", "market"))[0].tickers == ["AAA"]
    assert adapter._json_rows(View("community_top5", "community")) == []
    adapter.json_responses[-1].status = 403
    with pytest.raises(AccessDenied):
        adapter._json_rows(View("market_gain_live", "market"))


def test_site_text_content_blocks_and_korean_translation():
    row = {
        "title": "원문",
        "source": "reuters",
        "content": [{"type": "text", "content": "Full article"}],
        "translations": {
            "translated": {
                "ko_KR": {
                    "title": "번역 제목",
                    "content": [{"type": "text", "content": "전체 본문"}],
                }
            }
        },
    }
    item = jsonfeed.news_item(row, detail=True)
    assert item.title == "번역 제목" and item.body == "전체 본문"
    assert jsonfeed.body_text([{"type": "text", "content": "Full article"}]) == "Full article"


def test_empty_translation_keeps_available_original_body():
    row = {
        "title": "T",
        "content": [{"content": "Original body"}],
        "translations": {"translated": {"ko_KR": {"content": [{"content": ""}]}}},
    }
    assert jsonfeed.news_item(row, detail=True).body == "Original body"
