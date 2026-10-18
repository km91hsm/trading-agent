"""SEC EDGAR 수집기: 티커→CIK 맵(7일 캐시), companyfacts, DART 와 같은 키의 재무비율.

SEC 는 User-Agent "<이름> <이메일>" 을 요구한다. `filed <= asof` 인 10-K 값만 쓴다(룩어헤드 방지).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from trader.logging_setup import quiet_httpx
from trader.sources.dart import RATIO_KEYS, basic_ratios

logger = logging.getLogger("trader.sources")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
CACHE_DAYS = 7

# 공통 키 → (taxonomy, 후보 태그들, 기간형 여부)
_TAGS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "매출액": (
        "us-gaap",
        ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
        True,
    ),
    "영업이익": ("us-gaap", ("OperatingIncomeLoss",), True),
    "당기순이익": ("us-gaap", ("NetIncomeLoss", "ProfitLoss"), True),
    "자산총계": ("us-gaap", ("Assets",), False),
    "부채총계": ("us-gaap", ("Liabilities",), False),
    "자본총계": (
        "us-gaap",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        False,
    ),
}
_SHARES_TAG = ("dei", "EntityCommonStockSharesOutstanding", "shares")


class EdgarError(RuntimeError):
    """EDGAR HTTP 실패."""


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        *,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        quiet_httpx()
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._cache_dir = cache_dir
        self._http = http or httpx.Client(timeout=15)
        self._now = now
        self._tickers: dict[str, Any] | None = None

    def _get(self, url: str) -> Any:
        resp = self._http.get(url, headers=self._headers)
        if resp.status_code != 200:
            raise EdgarError(f"EDGAR HTTP {resp.status_code}: {url}")
        return resp.json()

    def _load_tickers(self) -> dict[str, Any]:
        if self._tickers is not None:
            return self._tickers
        cache = self._cache_dir / "edgar_tickers.json"
        now = self._now()
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if now - datetime.fromisoformat(cached["fetched_at"]) < timedelta(days=CACHE_DAYS):
                self._tickers = cached["map"]
                return self._tickers
        except (OSError, ValueError, KeyError, TypeError):
            pass
        data = self._get(TICKERS_URL)
        mapping: dict[str, Any] = {}
        for v in data.values() if isinstance(data, dict) else data:
            if isinstance(v, dict) and v.get("ticker") and v.get("cik_str") is not None:
                mapping[str(v["ticker"]).upper()] = {
                    "cik": f"{int(v['cik_str']):010d}",
                    "name": str(v.get("title") or "").strip(),
                }
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"fetched_at": now.isoformat(), "map": mapping}), encoding="utf-8"
        )
        tmp.replace(cache)
        self._tickers = mapping
        return mapping

    def cik(self, symbol: str) -> str | None:
        return (self._load_tickers().get(symbol.upper()) or {}).get("cik")

    def company_name(self, symbol: str) -> str | None:
        return (self._load_tickers().get(symbol.upper()) or {}).get("name") or None

    def companyfacts(self, cik: str) -> dict:
        return self._get(FACTS_URL.format(cik=cik))


def _annual_entries(entries: list[dict], asof: date, *, duration: bool) -> list[dict]:
    """asof 이전에 제출된 10-K 연간 값만, end 내림차순."""
    out = []
    for e in entries:
        if str(e.get("form", "")) != "10-K":
            continue
        filed = _d(e.get("filed"))
        end = _d(e.get("end"))
        if filed is None or end is None or filed > asof:
            continue
        if duration:
            start = _d(e.get("start"))
            if start is None or not (300 <= (end - start).days <= 400):
                continue
        out.append(e)
    out.sort(key=lambda e: (e["end"], e.get("filed", "")), reverse=True)
    return out


def financials_us(facts: dict, asof: date) -> tuple[dict[str, Decimal], int | None]:
    """companyfacts → 공통 키 재무제표(+`_전기`)와 보통주 수."""
    fin: dict[str, Decimal] = {}
    all_facts = facts.get("facts") or {}
    for key, (tax, tags, duration) in _TAGS.items():
        for tag in tags:
            units = ((all_facts.get(tax) or {}).get(tag) or {}).get("units") or {}
            entries = units.get("USD") or []
            annual = _annual_entries(entries, asof, duration=duration)
            if not annual:
                continue
            fin[key] = Decimal(str(annual[0]["val"]))
            latest_end = _d(annual[0]["end"])
            for prev in annual[1:]:
                prev_end = _d(prev["end"])
                if latest_end and prev_end and 300 <= (latest_end - prev_end).days <= 400:
                    fin[f"{key}_전기"] = Decimal(str(prev["val"]))
                    break
            break
    tax, tag, unit = _SHARES_TAG
    shares_entries = (((all_facts.get(tax) or {}).get(tag) or {}).get("units") or {}).get(
        unit
    ) or []
    shares = None
    annual = _annual_entries(shares_entries, asof, duration=False)
    if annual:
        shares = int(annual[0]["val"])
    return fin, shares


def ratios_us(
    edgar: EdgarClient, symbol: str, asof: date, price: Decimal | None
) -> dict[str, float | None]:
    cik = edgar.cik(symbol)
    if not cik:
        return dict.fromkeys(RATIO_KEYS)
    fin, shares = financials_us(edgar.companyfacts(cik), asof)
    return basic_ratios(fin, shares=shares, price=price)
