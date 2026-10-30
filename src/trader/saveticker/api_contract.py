"""Confirmed read-only browser contracts; no network clients or credentials."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from trader.saveticker import dom, jsonfeed
from trader.saveticker.models import Item

ORIGIN = "https://www.saveticker.com"
MAX_BYTES = 2 * 1024 * 1024
INDEX_SYMBOLS = ("QQQ", "SPY", "DIA", "IWM", "SOXX")


def descriptor(view):
    stream = view["stream"]
    if stream == "news_all":
        path, query, kind = (
            "/api/news/list",
            {
                "page": 1,
                "page_size": 20,
                "sort": "created_at_desc",
                "label_group": 1,
                "label_name": 1,
            },
            "news",
        )
    elif stream == "news_reports":
        path, query, kind = "/api/report/list", {"page": 1, "page_size": 20}, "reports"
    elif stream == "community_top5":
        path, query, kind = (
            "/api/stocks/api/v1/community/feed/ticker-rankings",
            {"limit": 5},
            "top5",
        )
    elif stream in {"community_index_live", "community_index_regular"}:
        path, query, kind = "/api/stocks/api/v1/market-panel", {}, "index"
    elif stream.startswith("calendar_") and re.fullmatch(r"\d{4}-\d{2}", view.get("month") or ""):
        first = date.fromisoformat(view["month"] + "-01")
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        path, query, kind = (
            "/api/calendar/events",
            {"start_date": str(first - timedelta(days=1)), "end_date": str(following)},
            "calendar",
        )
    else:
        # groupId necessity, popular regular basis and detail/PDF contracts are
        # not established. Never manufacture them from route names.
        return None
    return {
        "method": "GET",
        "url": ORIGIN + path + ("?" + urlencode(query) if query else ""),
        "kind": kind,
        "max_bytes": MAX_BYTES,
    }


def _rows(data, key, limit):
    rows = data.get(key)
    if (
        not isinstance(rows, list)
        or len(rows) > limit
        or any(not isinstance(x, dict) for x in rows)
    ):
        raise ValueError("api_shape_unverified")
    return rows


def parse_snapshot(view, response):
    expected = descriptor(view)
    if not expected or not isinstance(response, dict):
        raise ValueError("api_contract_unverified")
    if (
        response.get("url") != expected["url"]
        or type(response.get("status")) is not int
        or response["status"] != 200
    ):
        raise ValueError("api_response_unverified")
    if str(response.get("content_type", "")).split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("api_response_unverified")
    data = response.get("data")
    if (
        not isinstance(data, dict)
        or len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES
    ):
        raise ValueError("api_shape_unverified")
    kind = expected["kind"]
    key = {
        "news": "news_list",
        "reports": "reports",
        "calendar": "events",
        "index": "items",
        "top5": "rankings",
    }[kind]
    rows = _rows(data, key, 500 if kind == "calendar" else 50)
    if kind in {"news", "reports"}:
        # Unverified empty first pages must never erase a prior valid snapshot.
        if not rows and (type(data.get("total_count")) is not int or data["total_count"] != 0):
            raise ValueError("api_shape_unverified")
        if "total_count" in data and (
            type(data["total_count"]) is not int or data["total_count"] < len(rows)
        ):
            raise ValueError("api_shape_unverified")
        if kind == "reports" and (
            type(data.get("total_count")) is not int
            or data.get("page") != 1
            or data.get("page_size") != 20
        ):
            raise ValueError("api_shape_unverified")
    if kind == "calendar":
        query = parse_qs(urlsplit(expected["url"]).query)
        start, end = query["start_date"][0], query["end_date"][0]
        for row in rows:
            stamp = row.get("event_date")
            if not isinstance(stamp, str):
                raise ValueError("api_shape_unverified")
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if not start <= stamp[:10] <= end:
                raise ValueError("api_shape_unverified")
        if type(data.get("total_count")) is not int or data["total_count"] != len(rows):
            raise ValueError("api_shape_unverified")
        if any(
            not isinstance(x.get("id"), (str, int))
            or not isinstance(x.get("event_date"), str)
            or not x.get("title")
            for x in rows
        ):
            raise ValueError("api_shape_unverified")
        return jsonfeed.extract(data, view["stream"], month=view["month"])
    if kind == "reports":
        result = []
        for row in rows:
            match = re.fullmatch(
                r"(\d{4})년 (\d{1,2})월 (\d{1,2})일 \(.+\)", str(row.get("title", ""))
            )
            if not match or not isinstance(row.get("id"), (str, int)):
                raise ValueError("api_shape_unverified")
            day = date(*map(int, match.groups())).isoformat()
            result.append(
                Item(
                    id=dom.identity("daily_report", day),
                    title=row["title"],
                    report_date=day,
                    publisher="SAVE REPORT",
                    source_group="SAVE 리포트",
                    source_url=ORIGIN + "/news",
                    kind="report",
                    detail_status="pending",
                )
            )
        return result
    if kind == "news":
        if any(
            not x.get("id") or not isinstance(x.get("title"), str) or not x["title"] for x in rows
        ):
            raise ValueError("api_shape_unverified")
        return jsonfeed.extract(data, "news_all")
    if kind == "index":
        matched = {x.get("symbol"): x for x in rows if x.get("symbol") in INDEX_SYMBOLS}
        if len(matched) != 5 or len([x for x in rows if x.get("symbol") in INDEX_SYMBOLS]) != 5:
            raise ValueError("api_shape_unverified")
        rows = [matched[s] for s in INDEX_SYMBOLS]
    elif not rows or len(rows) > 5:
        raise ValueError("api_shape_unverified")
    for row in rows:
        symbol = (row.get("ticker") or row).get("symbol")
        quote = row.get("regular") if view["regular"] else row
        if (
            not isinstance(symbol, str)
            or not isinstance(quote, dict)
            or any(type(quote.get(k)) not in (int, float) for k in ("price", "changePercent"))
        ):
            raise ValueError("api_shape_unverified")
    return jsonfeed.extract({key: rows}, view["stream"], regular=view["regular"])
