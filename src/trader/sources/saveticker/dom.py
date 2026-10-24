"""브라우저가 표시한 DOM만 해석한다. 내부 API·숨겨진 앱 상태는 읽지 않는다."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup

from trader.config import KST
from trader.sources.saveticker.models import Item


def text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def tickers(value: str) -> list[str]:
    return sorted(set(re.findall(r"\$([A-Z][A-Z0-9.-]{0,15})\b", value)))


def authentication_required(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return any("로그인이 필요" in text(n) for n in soup.select('h2,h3,[role="dialog"]'))


def months_between(start: str, end: str) -> list[str]:
    a = date.fromisoformat(start + "-01")
    b = date.fromisoformat(end + "-01")
    if b < a or (b.year - a.year) * 12 + b.month - a.month > 24:
        raise ValueError("캘린더 범위는 순서대로 최대 25개월입니다")
    months = []
    while a <= b:
        months.append(a.strftime("%Y-%m"))
        a = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
    return months


def _news(soup) -> list[Item]:
    result = []
    for n in soup.select('#news-tabpanel [data-read][role="button"][aria-label]'):
        title = n.get("aria-label", "")
        top = n.find("div", recursive=False)
        publisher = text(top.select_one("div div")) if top else ""
        displayed = text(top.find("span", recursive=False)) if top else ""
        bottom = n.find_all("div", recursive=False)[-1]
        views = text(bottom.find_all("div", recursive=False)[-1])
        result.append(
            Item(
                id=identity("news", publisher, title),
                title=title,
                publisher=publisher,
                displayed_time=displayed or None,
                tickers=tickers(text(n)),
                views=views or None,
                kind="news",
            )
        )
    return result


def _posts(soup) -> list[Item]:
    result = []
    for menu in soup.select('button[aria-label="더보기 메뉴"]'):
        card = menu.parent.parent
        body = card.select_one("p.whitespace-pre-wrap")
        if not body:
            continue
        content = text(body)
        head = body.parent.find("div", recursive=False)
        spans = head.find_all("span") if head else []
        author = text(spans[0]) if spans else ""
        displayed = text(spans[-1]) if spans else ""
        controls = menu.parent.select("button:not([aria-label])")
        result.append(
            Item(
                id=identity("post", author, content),
                title=content[:120],
                body=content,
                author=author,
                tickers=tickers(content),
                displayed_time=displayed or None,
                likes=text(controls[0]) if controls else None,
                comment_count=text(controls[1]) if len(controls) > 1 else None,
                kind="post",
            )
        )
    return result


def _rankings(soup, stream: str) -> list[Item]:
    result = []
    # 사이드 패널 중복은 ID로 제거하고 화면 순서대로 순위를 붙인다.
    if stream.startswith(("community", "market")):
        label = {"index": "지수", "turnover": "거래대금", "gain": "상승", "loss": "하락"}.get(
            stream.split("_")[1]
        )
        trigger = next((n for n in soup.select("button") if text(n) == label), None)
        if trigger:
            for scope in trigger.parents:
                if scope.select('[role="button"][data-clickable], [data-stretched-link-card]'):
                    soup = scope
                    break
    cards = soup.select('[data-stretched-link-card], [role="button"][data-clickable]')
    basis_lines = [s for s in soup.stripped_strings if "기준" in s or "실시간" in s]
    basis = " · ".join(basis_lines[:6])
    for n in cards:
        value = text(n)
        price = re.search(r"\$[\d,]+(?:\.\d+)?", value)
        change = re.search(r"[+-]?\d+(?:\.\d+)?%", value)
        if not price or not change:
            continue
        heading = n.select_one("h3,h4")
        title = text(heading) or n.get("aria-label", "")
        spans = [text(s) for s in n.select("span") if not s.find("span")]
        symbol = next(
            (
                s.split(" · ")[0]
                for s in spans
                if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}(?: · 의견 \d+)?", s) and s != "ETF"
            ),
            "",
        )
        if heading:
            next_span = heading.find_next_sibling("span")
            candidate = text(next_span).split(" · ")[0]
            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", candidate):
                symbol = candidate
        if not symbol:
            match = re.search(r"\b([A-Z][A-Z0-9.-]{0,15})\b", value)
            symbol = match[1] if match else ""
        if not symbol:
            continue
        turnover = re.search(r"거래대금\s*(\$[\d.,]+[KMBT]?)", value)
        explicit_rank = next((int(s) for s in spans[:2] if s.isdigit()), None)
        result.append(
            Item(
                id=identity("symbol", symbol),
                title=title,
                tickers=[symbol],
                rank=explicit_rank or len(result) + 1,
                price=price[0],
                change_pct=change[0],
                turnover=turnover[1] if turnover else None,
                kind="quote",
                basis=basis,
            )
        )
    return result


def _calendar(soup, month: str) -> list[Item]:
    year = int(month[:4])
    result = []
    for heading in soup.select("h3"):
        day = re.fullmatch(r"(\d+)월 (\d+)일 .요일", text(heading))
        if not day:
            continue
        for n in heading.parent.select("[data-stretched-link-card]"):
            title = text(n.select_one("h4"))
            displayed = text(n.find("span", recursive=False))
            if not title:
                continue
            day_text = f"{year:04}-{int(day[1]):02}-{int(day[2]):02}"
            date_text = day_text + "T" + displayed + ":00"
            scheduled = None
            if re.fullmatch(r"\d{2}:\d{2}", displayed):
                scheduled = datetime.fromisoformat(date_text).replace(tzinfo=KST).astimezone(UTC)
            else:
                date_text = day_text + " " + (displayed or "시간 미정")
            p = n.select_one("p")
            values = text(p)
            forecast = re.search(r"예상:\s*([^\s)]+)", values)
            previous = re.search(r"이전:\s*([^\s)]+)", values)
            actual = values.split("(")[0].strip()
            result.append(
                Item(
                    id=identity("event", date_text, title),
                    title=title,
                    scheduled_at=scheduled,
                    scheduled_date=day_text,
                    displayed_time=date_text,
                    actual=actual or None,
                    forecast=forecast[1] if forecast else None,
                    previous=previous[1] if previous else None,
                    kind="event",
                )
            )
    return result


def extract(html: str, stream: str, *, month: str | None = None) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    if stream.startswith("news"):
        items = _news(soup)
    elif stream == "community_posts" or stream.startswith("community_ticker-"):
        items = _posts(soup)
    elif stream.startswith("calendar"):
        if not month:
            raise ValueError("캘린더 DOM에는 조회 월이 필요합니다")
        items = _calendar(soup, month)
    else:
        items = _rankings(soup, stream)
    return list({item.id: item for item in items}.values())


def news_detail(html: str, item: Item) -> Item:
    """열린 상세 화면에서 본문과 절대 게시 시각만 추출한다."""
    soup = BeautifulSoup(html, "html.parser")
    heading = next((n for n in soup.select("h2") if text(n) == item.title), None)
    if not heading:
        return item.model_copy(update={"detail_status": "unavailable"})
    # 상세 패널의 제목을 감싼 본문 영역. 제목 뒤에 표시된 번역·원문을 보존한다.
    panel = heading.parent
    for _ in range(4):
        if "기사 원문 등록 시간" in text(panel):
            break
        panel = panel.parent or panel
    value = panel.get_text("\n", strip=True)
    match = re.search(
        r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})",
        value,
    )
    published = None
    if match:
        published = datetime(*map(int, match.groups()), tzinfo=KST).astimezone(UTC)
    content = value.split(item.title, 1)[-1]
    content = content.split("기사 원문 등록 시간")[0].strip()
    return item.model_copy(
        update={
            "body": content,
            "published_at": published,
            "detail_status": "ok" if content else "unavailable",
        }
    )


def comments(html: str, *, parent_id: str) -> list[Item]:
    """열린 댓글·답글만 추출하고 원글 식별자를 붙인다."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for node in soup.select('[data-type="comment"], [data-type="reply"]'):
        body = text(node.select_one("p"))
        author = text(node.select_one("span.text-small-bold"))
        if not body:
            continue
        spans = node.select("span.text-neutral-600")
        displayed = text(spans[0]) if spans else None
        result.append(
            Item(
                id=identity(parent_id, author, body),
                parent_id=parent_id,
                title=body[:120],
                body=body,
                author=author,
                displayed_time=displayed,
                tickers=tickers(body),
                likes=text(node.select_one('button[aria-label="좋아요"]')) or None,
                kind="comment",
            )
        )
    return list({item.id: item for item in result}.values())


def calendar_month(html: str) -> str:
    """월별 범위의 이전 버튼 옆 연월을 읽는다(좌측 달력 격자와 구분)."""
    soup = BeautifulSoup(html, "html.parser")
    button = soup.select_one('button[aria-label="이전"]')
    if not button:
        button = next((n for n in soup.select("button") if text(n) == "이전"), None)
    if not button:
        raise ValueError("calendar_range_control_missing")
    match = re.search(r"(\d{4})년\s*(\d{1,2})월", text(button.parent))
    if not match:
        raise ValueError("calendar_month_unknown")
    return f"{int(match[1]):04}-{int(match[2]):02}"


def news_sources(html: str) -> list[str]:
    """텍스트 없는 로고 탭도 접근 가능한 이름으로 읽는다."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for node in soup.select('[role="tab"]'):
        image = node.select_one("img[alt]")
        name = node.get("aria-label") or text(node) or (image.get("alt") if image else "")
        if name:
            result.append(name)
    return result
