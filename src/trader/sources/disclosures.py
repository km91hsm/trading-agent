"""공시 중요도 (Phase 25 단계 6). DART 보고서명 키워드·EDGAR 8-K item 으로 severity 를 매기고
뉴스 저장소(`news_items.severity`, source dart|edgar)에 적재한다. critical 은 데몬
`_news_trigger` 가 새 뉴스 건수 조건 없이 즉시 재분석하고 `[중요 공시]` 를 알리며, 운영 점검
`disclosure` 영역이 보유 종목의 최근 critical 을 특보로 올린다. 판정은 파이썬 규칙만."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from trader.config import KST

logger = logging.getLogger("trader.sources.disclosures")

SEVERITIES = ("critical", "high", "info")
# 상장 유지·존속을 흔드는 사건 → critical, 희석·지배구조·부정 → high
DART_CRITICAL = (
    "감자",
    "거래정지",
    "매매거래정지",
    "관리종목",
    "상장폐지",
    "회생절차",
    "파산",
    "감사의견거절",
    "의견거절",
    "부적정",
)
DART_HIGH = ("유상증자", "전환사채", "최대주주변경", "횡령", "배임", "불성실공시")
# 8-K: 1.03 파산·법정관리, 3.01 상장폐지 통지, 4.02 과거 재무제표 신뢰 철회 → critical,
# 1.05 중대 사이버 사고 → high, 2.02 실적 발표 → info
EDGAR_ITEMS = {
    "1.03": "critical",
    "3.01": "critical",
    "4.02": "critical",
    "1.05": "high",
    "2.02": "info",
}
EDGAR_LOOKBACK_DAYS = 3


def _rank(sev: str | None) -> int:
    return SEVERITIES.index(sev) if sev in SEVERITIES else len(SEVERITIES)


def classify_dart(report_nm: str) -> str | None:
    t = "".join((report_nm or "").split())
    if any(k in t for k in DART_CRITICAL):
        return "critical"
    if any(k in t for k in DART_HIGH):
        return "high"
    return None


def classify_8k(items: list[str]) -> str | None:
    best: str | None = None
    for it in items:
        sev = EDGAR_ITEMS.get(it.strip())
        if sev is not None and _rank(sev) < _rank(best):
            best = sev
    return best


def _dart_items(dart: Any, day: date, tickers: set[str]) -> list[dict]:
    out = []
    for r in dart.market_disclosures(day):
        code = str(r.get("stock_code") or "").strip()
        if not code or code not in tickers:
            continue
        name = str(r.get("report_nm") or "").strip()
        raw = str(r.get("rcept_dt") or "")
        try:
            pub = datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:8]), tzinfo=KST).isoformat()
        except ValueError:
            pub = None
        out.append(
            {
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no', '')}",
                "title": f"[공시] {str(r.get('corp_name') or code).strip()} {name}",
                "published_at": pub,
                "tickers": [code],
                "severity": classify_dart(name),
            }
        )
    return out


def _edgar_items(edgar: Any, symbol: str, since: date) -> list[dict]:
    cik = edgar.cik(symbol)
    if not cik:
        return []
    recent = ((edgar.submissions(cik) or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if not str(form).startswith("8-K"):
            continue
        try:
            filed = date.fromisoformat(str(recent["filingDate"][i]))
        except (KeyError, IndexError, ValueError):
            continue
        if filed < since:
            continue
        items = [
            x for x in str((recent.get("items") or [""] * len(forms))[i] or "").split(",") if x
        ]
        acc = str(recent["accessionNumber"][i])
        doc = str((recent.get("primaryDocument") or [""] * len(forms))[i] or "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}"
        pub = datetime(filed.year, filed.month, filed.day, tzinfo=UTC)
        out.append(
            {
                "url": f"{base}/{acc.replace('-', '')}/{doc}",
                "title": f"[8-K] {symbol} items {', '.join(items) or '-'}",
                "published_at": pub.isoformat(),
                "tickers": [symbol],
                "severity": classify_8k(items),
            }
        )
    return out


def collect(
    conn: sqlite3.Connection,
    *,
    market: str,
    tickers: set[str],
    now: datetime,
    dart: Any = None,
    edgar: Any = None,
) -> dict:
    """대상 종목(보유·유니버스·관심)의 오늘 공시를 적재. 반환 `{inserted, critical, error?}`.
    실패는 예외 대신 error 로 — 뉴스 수집 잡을 막지 않는다."""
    from trader.sources import news_store

    items: list[dict] = []
    try:
        if market == "KR" and dart is not None:
            items = _dart_items(dart, now.astimezone(KST).date(), tickers)
            source, lang = "dart", "ko"
        elif market == "US" and edgar is not None:
            since = now.date() - timedelta(days=EDGAR_LOOKBACK_DAYS)
            for t in sorted(tickers):
                try:
                    items += _edgar_items(edgar, t, since)
                except Exception as e:  # noqa: BLE001 - 종목 하나의 실패는 건너뛴다
                    logger.debug("EDGAR 제출 목록 실패 %s: %s", t, e)
            source, lang = "edgar", "en"
        else:
            return {"inserted": 0, "critical": 0, "skipped": "클라이언트 없음"}
    except Exception as e:  # noqa: BLE001
        logger.warning("공시 수집 실패 %s: %s", market, e)
        return {"inserted": 0, "critical": 0, "error": f"{type(e).__name__}: {e}"}
    n = news_store.insert_items(
        conn,
        items,
        market=market,
        source=source,
        query="disclosure",
        lang=lang,
        names={},
        extra_tickers=True,
    )
    return {"inserted": n, "critical": sum(1 for i in items if i["severity"] == "critical")}


def critical_recent(
    conn: sqlite3.Connection, market: str, *, since: datetime, tickers: set[str] | None = None
) -> list[dict]:
    """since 이후 적재된 critical 공시(종목 필터 선택). 운영 점검·즉시 재분석이 쓴다."""
    import json

    rows = conn.execute(
        "SELECT id, title, url, tickers_json, published_at, received_at FROM news_items "
        "WHERE market=? AND severity='critical' AND received_at >= ? ORDER BY id",
        (market, since.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        ts = json.loads(r["tickers_json"] or "[]")
        if tickers is not None and not (set(ts) & tickers):
            continue
        out.append({**dict(r), "tickers": ts})
    return out
