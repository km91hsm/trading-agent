"""정상 브라우저가 수신한 허용 JSON만 파일 계약으로 변환한다."""

from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from trader.saveticker.dom import identity
from trader.saveticker.models import Item

ROUTES = {
    "/api/news/list": "news",
    "/api/news/detail": "detail",
    "/api/calendar/events": "calendar",
    "/api/stocks/api/v1/tickers/popular": "popular",
    "/api/stocks/api/v1/market-panel": "index",
    "/api/stocks/api/v1/community/feed/ticker-rankings": "top5",
}


def route(url):
    parsed = urlsplit(url)
    return (
        ROUTES.get(parsed.path)
        if parsed.scheme == "https" and parsed.hostname == "www.saveticker.com"
        else None
    )


def body_text(value):
    if isinstance(value, str):
        return BeautifulSoup(value, "html.parser").get_text()
    if isinstance(value, list):
        return "\n".join(filter(None, (body_text(x) for x in value)))
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"])
        children = value.get("children", value.get("content", value.get("body", [])))
        if isinstance(children, str):
            return body_text(children)
        if isinstance(children, list):
            return "".join(body_text(x) for x in children)
    return ""


def aware(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=ZoneInfo("Asia/Seoul"))


def scalar(value):
    return str(value) if isinstance(value, (str, int, float)) else None


def news_item(row, *, detail=False):
    translated = (row.get("translations") or {}).get("translated", {}).get("ko_KR", {})
    title = translated.get("title") or row.get("title", "")
    publisher = row.get("source") or row.get("channel") or ""
    if isinstance(publisher, dict):
        publisher = publisher.get("name", "")
    publisher = {
        "reuters": "로이터",
        "financial-juice": "파이낸셜주스",
        "save-pick": "SAVE PICK",
        "save_pick": "SAVE PICK",
    }.get(str(publisher), publisher)
    labels = row.get("content_labels") or []
    categories = [
        str(x.get("name") or x.get("label_name") or "") if isinstance(x, dict) else str(x)
        for x in labels
    ]
    headline = bool(row.get("is_headline_only"))
    body = (body_text(translated.get("content")) or body_text(row.get("content"))) if detail else ""
    return Item(
        id=identity("news", publisher, title),
        title=title,
        publisher=str(publisher),
        source_group={"Reuters": "로이터", "로이터": "로이터", "파이낸셜주스": "속보"}.get(
            str(publisher), str(publisher) or None
        ),
        categories=[x for x in categories if x],
        tickers=[
            x["symbol"] for x in row.get("tickers", []) if isinstance(x, dict) and x.get("symbol")
        ],
        is_breaking="속보" in categories,
        body=body,
        published_at=aware(row.get("created_at")),
        kind="news",
        detail_status="ok" if body or headline else "pending",
    )


def extract(value, stream, *, month=None, regular=False):
    if not isinstance(value, dict):
        raise ValueError("invalid_json_shape")
    if stream == "news_all":
        return [news_item(x) for x in value.get("news_list", [])]
    if stream.startswith("calendar_"):
        result = []
        for row in value.get("events", []):
            stamp = str(row.get("event_date") or "")
            day = stamp[:10]
            if not month or day[:7] != month:
                continue
            result.append(
                Item(
                    id=identity("event-json", str(row["id"])),
                    title=row.get("title", ""),
                    body=body_text(row.get("content")),
                    scheduled_date=day,
                    scheduled_at=None if row.get("event_date_only") else aware(stamp),
                    kind="event",
                )
            )
        return result
    result = []
    for index, row in enumerate(value.get("rankings", value.get("items", []))):
        ticker = row.get("ticker") or row
        if isinstance(ticker, str):
            ticker = {"symbol": ticker}
        symbol = ticker.get("symbol") or row.get("symbol")
        if not symbol:
            continue
        quote = row.get("regular", row) if regular else row
        quote = quote if isinstance(quote, dict) else {}
        result.append(
            Item(
                id=identity("symbol", symbol),
                title=ticker.get("nameKo") or row.get("nameKo") or row.get("labelKo") or symbol,
                tickers=[symbol],
                rank=row.get("rank") or index + 1,
                price=scalar(quote.get("price")),
                change_pct=scalar(quote.get("changePercent")),
                turnover=scalar(quote.get("turnover")),
                kind="quote",
                basis="regular" if regular else "live",
            )
        )
    return result
