"""세이브티커 로컬 수집 폴더 리더 (Phase 19 이연 회수).

세이브티커(saveticker.com)는 Cloudflare 봇 검증 + robots AI 크롤러 금지라 이 저장소는 사이트를 긁지
않는다. 대신 사용자의 별도 수집기가 **이 프로젝트의 `state/external/saveticker/`** 에 쌓는 파일을
읽기만 한다(`sources.yaml news.saveticker_dir`, 폴더가 없으면 조용히 건너뜀). 구조(수집기 v1):

- `data/<YYYY-MM-DD>/{news,community,market,calendar}.jsonl` — 한 줄 = 변경 이력 하나
  `{revision, stream, received_at(UTC), item, view}`
- `data/latest/<stream>.json` — 스트림별 최신 화면 `{stream, observed_at, status, items, view}`
- `data/attachments/<sha>.pdf|.txt` — SAVE 데일리 리포트 원본·추출 텍스트

뉴스(`news_all`·`news_reports`)는 시장 전체 뉴스 저장소(`news_items`, source `saveticker:<출처>`)에
멱등 적재되어 발굴·시장 세션·종목 세션이 다른 뉴스와 같은 길로 읽는다. 종목 TOP5·지수·상승/하락/
거래대금·캘린더·리포트 발췌는 `snapshot(asof)` 로 세션 입력(`saveticker` 절)에 들어간다(커뮤니티
트렌드는 2026-10-26 수집 중단이라 읽지 않는다).
룩어헤드: `observed_at`/`received_at` 이 asof 이하인 것만. 신선도: latest 화면은 수집기가 매 회차
`observed_at` 을 새로 쓰므로, asof 보다 `STALE_AFTER` 넘게 오래된 화면은 수집기가 멈춘 것으로 보고
뺀다(`snapshot.stale` 에 스트림 이름).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.config import KST
from trader.platform import paths

logger = logging.getLogger("trader.sources")

DEFAULT_DIR = "state/external/saveticker"
NEWS_STREAMS = ("news_all", "news_reports")
REPORT_EXCERPT_CHARS = 1500
SUMMARY_CHARS = 300
JSONL_DAYS = 3  # 뉴스 적재는 최근 N일 폴더만 다시 읽는다(URL 해시로 멱등)
CALENDAR_BACK_DAYS = 1
CALENDAR_AHEAD_DAYS = 7
# 화면 신선도: 순위·TOP5 는 20초~5분 주기라 1시간, 리포트(15분)는 날 단위라 하루, 캘린더는 예정
# 일정이라 오래된 관측도 쓸모가 있어 2주
STALE_AFTER = timedelta(hours=1)
STALE_AFTER_BY_PREFIX = (("calendar_", timedelta(days=14)), ("news_reports", timedelta(days=1)))
_KR_CODE = re.compile(r"^\d{6}$")
_DISPLAYED = re.compile(r"^(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{1,2}):(\d{2})$")


def resolve_dir(configured: str | None) -> Path | None:
    """설정 경로(상대면 프로젝트 루트 기준). None/빈 문자열이면 None."""
    if not configured:
        return None
    p = Path(configured).expanduser()
    if not p.is_absolute():
        p = paths.project_root() / p
    return p


def available(root: Path | None) -> bool:
    return bool(root) and (root / "data").is_dir()  # type: ignore[operator]


def _parse_dt(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def published_at_of(item: dict) -> str | None:
    """`published_at`(UTC) 우선, 없으면 화면 시각 '2026. 10. 24. 07:40'(KST).
    상대시각('2시간 전')만 있으면 None."""
    dt = _parse_dt(item.get("published_at"))
    if dt is not None:
        return dt.isoformat()
    shown = str(item.get("displayed_time") or "").strip()
    m = _DISPLAYED.match(shown)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=KST).astimezone(UTC).isoformat()
        except ValueError:
            return None
    iso = _parse_dt(shown)
    return iso.isoformat() if iso else None


def markets_of(item: dict) -> list[str]:
    """티커가 전부 6자리 숫자면 KR, 영문 심볼이 있으면 US, 티커가 없으면 둘 다(거시·속보)."""
    tickers = [str(t).strip() for t in item.get("tickers") or [] if str(t).strip()]
    if not tickers:
        return ["KR", "US"]
    kr = [t for t in tickers if _KR_CODE.match(t)]
    us = [t for t in tickers if not _KR_CODE.match(t)]
    out = []
    if kr:
        out.append("KR")
    if us:
        out.append("US")
    return out


def _report_excerpt(root: Path, item: dict, limit: int = REPORT_EXCERPT_CHARS) -> str:
    att = item.get("attachment") or {}
    rel = att.get("text_path")
    if not rel:
        return ""
    p = (root / "data" / rel).resolve()
    if (root / "data").resolve() not in p.parents or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 추출 텍스트는 토큰 단위 줄바꿈이 많다 → 공백으로 접는다
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _jsonl_dates(root: Path, *, today: date, days: int) -> list[Path]:
    out = []
    for i in range(-1, days):  # 수집 PC 가 KST 자정을 먼저 넘기면 '내일' 폴더가 있을 수 있다
        d = root / "data" / (today - timedelta(days=i)).isoformat()
        if d.is_dir():
            out.append(d)
    return out


def read_news_items(root: Path, *, today: date, days: int = JSONL_DAYS) -> list[dict]:
    """최근 N일 `news.jsonl` 의 뉴스·리포트 항목(같은 id 는 마지막 revision 만).
    `_received_at`·`_stream`·`_revision` 을 덧붙인다."""
    latest: dict[str, dict] = {}
    for d in _jsonl_dates(root, today=today, days=days):
        p = d / "news.jsonl"
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            logger.warning("세이브티커 파일 읽기 실패 %s: %s", p, e)
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("stream") not in NEWS_STREAMS:
                continue
            item = row.get("item") or {}
            iid = str(item.get("id") or "")
            if not iid:
                continue
            prev = latest.get(iid)
            if prev is None or int(row.get("revision") or 0) >= int(prev.get("_revision") or 0):
                latest[iid] = {
                    **item,
                    "_revision": row.get("revision"),
                    "_received_at": row.get("received_at"),
                    "_stream": row.get("stream"),
                }
    return list(latest.values())


def to_store_item(root: Path, item: dict) -> dict:
    """세이브티커 항목 → news_items 적재용 dict (`news_store.insert_items` 입력)."""
    is_report = item.get("kind") == "report" or item.get("_stream") == "news_reports"
    title = str(item.get("title") or "").strip()
    publisher = str(item.get("publisher") or item.get("source_group") or "saveticker").strip()
    if is_report:
        title = f"SAVE 데일리 리포트 {item.get('report_date') or title}".strip()
        summary = _report_excerpt(root, item, SUMMARY_CHARS)
        published = (
            datetime.fromisoformat(f"{item['report_date']}T08:00:00+09:00")
            .astimezone(UTC)
            .isoformat()
            if item.get("report_date")
            else None
        )
    else:
        summary = re.sub(r"\s+", " ", str(item.get("body") or "")).strip()[:SUMMARY_CHARS]
        published = published_at_of(item)
    # 리포트의 source_url 은 목록 페이지(전부 같다) → 날짜별 합성 URL 로 멱등 키를 만든다
    if is_report:
        url = f"https://www.saveticker.com/news#report-{item.get('report_date') or item.get('id')}"
    else:
        url = (item.get("source_url") or "").strip() or (
            f"https://www.saveticker.com/news#{item.get('id')}"
        )
    return {
        "title": title,
        "summary": summary,
        "url": url,
        "published_at": published,
        "received_at": item.get("_received_at"),
        "tickers": [str(t) for t in item.get("tickers") or []],
        "publisher": publisher,
        "categories": list(item.get("categories") or []),
        "stream": item.get("_stream"),
    }


def ingest_news(
    conn: sqlite3.Connection,
    root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    names_by_market: dict[str, dict[str, str]] | None = None,
    days: int = JSONL_DAYS,
) -> dict:
    """뉴스·리포트를 시장 전체 뉴스 저장소에 멱등 적재한다. 폴더가 없으면 skipped."""
    from trader.sources import news_store

    if not available(root):
        return {"skipped": "saveticker 폴더 없음"}
    today = now().astimezone(KST).date()
    items = read_news_items(root, today=today, days=days)
    inserted = 0
    by_market: dict[str, int] = {}
    for raw in items:
        st = to_store_item(root, raw)
        if not st["title"]:
            continue
        for market in markets_of(raw):
            n = news_store.insert_items(
                conn,
                [st],
                market=market,
                source=f"saveticker:{st['publisher']}",
                query=str(st.get("stream") or "news"),
                lang="ko",
                names=(names_by_market or {}).get(market, {}),
                extra_tickers=True,
            )
            inserted += n
            by_market[market] = by_market.get(market, 0) + n
    return {"items": len(items), "inserted": inserted, "by_market": by_market}


# --- 스냅샷(세션 입력) -------------------------------------------------------------------


SCHEMA_VERSION = (
    1  # 사용자 수집기의 파일 schema_version. 다르면 경고 1회(프로세스) 뒤 그대로 읽는다
)
_schema_warned: set[int] = set()


def _check_schema(snap: dict | None, where: str) -> None:
    v = (snap or {}).get("schema_version")
    if v is None or v == SCHEMA_VERSION or v in _schema_warned:
        return
    _schema_warned.add(v)
    logger.warning(
        "세이브티커 schema_version %s (리더는 %s 기준) %s — 필드가 달라졌을 수 있어 리더를 맞춘다",
        v,
        SCHEMA_VERSION,
        where,
    )


def _load_latest(root: Path, stream: str) -> dict | None:
    p = root / "data" / "latest" / f"{stream}.json"
    if not p.is_file():
        return None
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("세이브티커 latest 읽기 실패 %s: %s", p.name, e)
        return None
    _check_schema(snap, p.name)
    return snap


def _calendar(
    root: Path,
    cutoff: datetime,
    *,
    back_days: int,
    ahead_days: int,
    stale: set[str] | None = None,
) -> list[dict]:
    """경제 일정(월 파일 여러 개) 중 cutoff-back ~ cutoff+ahead 창, scheduled_at 오름차순."""
    lo = cutoff - timedelta(days=back_days)
    hi = cutoff + timedelta(days=ahead_days)
    span = (hi - lo).days + 2
    months = sorted({(lo + timedelta(days=i)).strftime("%Y-%m") for i in range(0, span)})
    out: list[dict] = []
    for ym in months:
        for it in _items(_load_latest(root, f"calendar_{ym}"), cutoff, stale):
            when = _parse_dt(it.get("scheduled_at"))
            if when is None or not (lo <= when <= hi):
                continue
            out.append(
                {
                    "scheduled_at": when.isoformat(),
                    "title": it.get("title"),
                    "actual": it.get("actual"),
                    "forecast": it.get("forecast"),
                    "previous": it.get("previous"),
                }
            )
    out.sort(key=lambda r: r["scheduled_at"])
    return out


def upcoming(root: Path | None, *, asof: datetime, days: int = 7) -> list[dict]:
    """종목 세션 `news_macro.events.upcoming` 용: asof ~ +days 의 경제 일정(세이브티커 캘린더).
    폴더가 없으면 빈 목록. 항목은 {date(KST), kind: "macro", title, forecast, previous, source}."""
    if root is None or not available(root):
        return []
    cutoff = asof.astimezone(UTC)
    out = []
    for c in _calendar(root, cutoff, back_days=0, ahead_days=days):
        when = _parse_dt(c["scheduled_at"])
        if when is None or when < cutoff:
            continue
        out.append(
            {
                "date": when.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                "kind": "macro",
                "title": c.get("title"),
                "forecast": c.get("forecast"),
                "previous": c.get("previous"),
                "source": "saveticker",
            }
        )
    return out


def _items(snap: dict | None, asof: datetime, stale: set[str] | None = None) -> list[dict]:
    if not snap:
        return []
    observed = _parse_dt(snap.get("observed_at"))
    cutoff = asof.astimezone(UTC)
    if observed is not None and observed > cutoff:
        return []  # asof 뒤 화면은 보이지 않는다(룩어헤드)
    stream = str(snap.get("stream") or "")
    limit = next((v for k, v in STALE_AFTER_BY_PREFIX if stream.startswith(k)), STALE_AFTER)
    if observed is not None and cutoff - observed > limit:
        if stale is not None:
            stale.add(stream)
        return []  # 수집기가 멈춘 뒤 남은 화면을 '지금' 값으로 쓰지 않는다
    return list(snap.get("items") or [])


def _quote_rows(items: list[dict], *, with_turnover: bool = False) -> list[dict]:
    out = []
    for it in items:
        row = {
            "rank": it.get("rank"),
            "name": it.get("title"),
            "tickers": list(it.get("tickers") or []),
            "change_pct": it.get("change_pct"),
            "price": it.get("price"),
            "basis": it.get("basis"),
        }
        if with_turnover:
            row["turnover"] = it.get("turnover")
        if it.get("views"):
            row["views"] = it.get("views")
        out.append(row)
    return out


def snapshot(root: Path | None, *, asof: datetime) -> dict | None:
    """세션 입력용 요약(토큰 절약: 항목 수 그대로, 필드만 고른다). 폴더가 없으면 None."""
    if root is None or not available(root):
        return None
    cutoff = asof.astimezone(UTC)
    stale: set[str] = set()

    def latest(stream: str) -> list[dict]:
        return _items(_load_latest(root, stream), cutoff, stale)

    index = _quote_rows(latest("community_index_regular"))
    top5 = _quote_rows(latest("community_top5"))
    movers = {
        key: _quote_rows(latest(f"market_{key}_regular"), with_turnover=True)
        for key in ("gain", "loss", "turnover")
    }
    community = {key: _quote_rows(latest(f"community_{key}_regular")) for key in ("gain", "loss")}
    # 캘린더: asof 기준 -1일 ~ +7일 일정 (월 파일 여러 개)
    calendar = _calendar(
        root, cutoff, back_days=CALENDAR_BACK_DAYS, ahead_days=CALENDAR_AHEAD_DAYS, stale=stale
    )
    report = None
    reports = latest("news_reports")
    if reports:
        latest_r = max(reports, key=lambda r: str(r.get("report_date") or ""))
        report = {
            "report_date": latest_r.get("report_date"),
            "title": latest_r.get("title"),
            "excerpt": _report_excerpt(root, latest_r),
        }
    status = _load_latest(root, "../status") or {}
    observed = [
        _parse_dt(s.get("observed_at"))
        for s in (
            _load_latest(root, "community_top5"),
            _load_latest(root, "market_gain_regular"),
            _load_latest(root, "news_all"),
        )
        if s
    ]
    observed = [o for o in observed if o is not None and o <= cutoff]
    return {
        "source": "saveticker (로컬 수집기, news.saveticker_dir)",
        "observed_at": max(observed).isoformat() if observed else None,
        "index": index,
        "community_top5": top5,
        "community": community,
        "movers": movers,
        "calendar": calendar,
        "report": report,
        "stale": sorted(stale) or None,
        "streams_status": {
            k: v.get("status")
            for k, v in (status.get("streams") or {}).items()
            if isinstance(v, dict) and k != "community_trends"
        }
        or None,
    }


def for_ticker(snap: dict | None, ticker: str) -> dict | None:
    """종목 세션 분위기 절: 이 종목이 등장하는 TOP5·상승/하락·거래대금 행 + 시장 지수."""
    if not snap:
        return None
    t = ticker.upper()

    def hits(rows: list[dict]) -> list[dict]:
        return [r for r in rows if t in [str(x).upper() for x in r.get("tickers") or []]]

    mentions = {
        "community_top5": hits(snap.get("community_top5") or []),
        "community_gain": hits((snap.get("community") or {}).get("gain") or []),
        "community_loss": hits((snap.get("community") or {}).get("loss") or []),
        "market_gain": hits((snap.get("movers") or {}).get("gain") or []),
        "market_loss": hits((snap.get("movers") or {}).get("loss") or []),
        "market_turnover": hits((snap.get("movers") or {}).get("turnover") or []),
    }
    return {
        "observed_at": snap.get("observed_at"),
        "mentions": {k: v for k, v in mentions.items() if v},
        "index": snap.get("index") or [],
        "note": (
            "종목 언급(mentions)이 비어 있으면 세이브티커 커뮤니티·시세 상위에 없다는 뜻. "
            "index 는 시장 전체 분위기 맥락이다"
        ),
    }
