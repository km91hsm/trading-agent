"""DART Open API 수집기: 고유번호 맵(7일 캐시), 재무제표, 주식수, 공시 목록, 재무비율.

인증키는 params 로만 전달하고 로그·예외 메시지에는 넣지 않는다.
"""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("trader.sources")

BASE = "https://opendart.fss.or.kr/api"
CACHE_DAYS = 7
ANNUAL = "11011"
RATIO_KEYS = ("매출성장률", "영업이익률", "ROE", "부채비율", "PER", "PBR", "PSR")

# DART 계정명 → 공통 키. EDGAR 수집기도 같은 공통 키로 basic_ratios() 를 공유한다.
_ACCOUNT_PATTERNS: dict[str, tuple[str, ...]] = {
    "매출액": ("매출액", "수익(매출액)", "영업수익"),
    "영업이익": ("영업이익", "영업이익(손실)"),
    "당기순이익": ("당기순이익", "당기순이익(손실)", "당기순손익"),
    "자산총계": ("자산총계",),
    "부채총계": ("부채총계",),
    "자본총계": ("자본총계",),
}


class DartError(RuntimeError):
    """DART 응답 status ≠ 000 (013 조회 결과 없음은 예외가 아니라 빈 결과)."""


def _quiet_httpx() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _dec(v: Any) -> Decimal | None:
    s = str(v or "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def fiscal_year_for(asof: date) -> int:
    """asof 시점에 확정돼 있는 최신 사업연도: 4월 1일부터 전년도, 그 전엔 2년 전 (룩어헤드 방지)."""
    return asof.year - 1 if asof.month >= 4 else asof.year - 2


class DartClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        *,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        _quiet_httpx()
        self._key = api_key
        self._cache_dir = cache_dir
        self._http = http or httpx.Client(timeout=15)
        self._now = now
        self._corp_map: dict[str, dict[str, str]] | None = None

    # --- 공통 ---------------------------------------------------------------

    def _get_json(self, endpoint: str, **params: str) -> dict:
        resp = self._http.get(f"{BASE}/{endpoint}", params={"crtfc_key": self._key, **params})
        if resp.status_code != 200:
            raise DartError(f"DART HTTP {resp.status_code} ({endpoint})")
        data = resp.json()
        status = str(data.get("status", ""))
        if status == "013":
            return {"status": status, "list": []}
        if status != "000":
            msg = str(data.get("message", "")).replace(self._key, "***")
            logger.warning("DART 오류 %s (%s): %s", status, endpoint, msg)
            raise DartError(f"DART status {status} ({endpoint}): {msg}")
        return data

    # --- 고유번호 -----------------------------------------------------------

    def _load_corp_map(self) -> dict[str, dict[str, str]]:
        if self._corp_map is not None:
            return self._corp_map
        cache = self._cache_dir / "dart_corpcode.json"
        now = self._now()
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if now - fetched < timedelta(days=CACHE_DAYS):
                self._corp_map = cached["map"]
                return self._corp_map
        except (OSError, ValueError, KeyError, TypeError):
            pass
        resp = self._http.get(f"{BASE}/corpCode.xml", params={"crtfc_key": self._key})
        if resp.status_code != 200:
            raise DartError(f"DART corpCode HTTP {resp.status_code}")
        if not zipfile.is_zipfile(io.BytesIO(resp.content)):
            # 키 오류·한도 초과·점검(800) 이면 zip 이 아니라 오류 XML(<result><status>) 이 온다
            try:
                err = ET.fromstring(resp.content)
                status = (err.findtext("status") or "?").strip()
                msg = (err.findtext("message") or "").strip().replace(self._key, "***")
            except ET.ParseError:
                status, msg = "?", "zip 이 아닌 응답"
            logger.warning("DART corpCode 오류 %s: %s", status, msg)
            raise DartError(f"DART status {status} (corpCode): {msg}")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml_bytes = z.read(z.namelist()[0])
        root = ET.fromstring(xml_bytes)
        mapping: dict[str, dict[str, str]] = {}
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if not stock:
                continue
            mapping[stock] = {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
            }
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"fetched_at": now.isoformat(), "map": mapping}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(cache)
        self._corp_map = mapping
        return mapping

    def corp_code(self, ticker: str) -> str | None:
        return (self._load_corp_map().get(ticker) or {}).get("corp_code")

    def corp_name(self, ticker: str) -> str | None:
        return (self._load_corp_map().get(ticker) or {}).get("corp_name")

    # --- 재무제표 -----------------------------------------------------------

    def financials(self, corp_code: str, year: int, reprt_code: str = ANNUAL) -> dict[str, Decimal]:
        """연결재무제표 주요 계정 (공통 키 + `_전기`). 없으면 빈 dict."""
        data = self._get_json(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=str(year),
            reprt_code=reprt_code,
            fs_div="CFS",
        )
        out: dict[str, Decimal] = {}
        for row in data.get("list") or []:
            name = re.sub(r"\s+", "", str(row.get("account_nm", "")))
            sj = str(row.get("sj_div", ""))
            for key, patterns in _ACCOUNT_PATTERNS.items():
                if key in out or name not in patterns:
                    continue
                if key in ("매출액", "영업이익", "당기순이익") and sj not in ("IS", "CIS"):
                    continue
                cur = _dec(row.get("thstrm_amount"))
                if cur is None:
                    continue
                out[key] = cur
                prev = _dec(row.get("frmtrm_amount"))
                if prev is not None:
                    out[f"{key}_전기"] = prev
        return out

    def shares(self, corp_code: str, year: int, reprt_code: str = ANNUAL) -> int | None:
        """보통주 발행주식총수."""
        data = self._get_json(
            "stockTotqySttus.json", corp_code=corp_code, bsns_year=str(year), reprt_code=reprt_code
        )
        for row in data.get("list") or []:
            if "보통" in str(row.get("se", "")):
                v = _dec(row.get("istc_totqy")) or _dec(row.get("distb_stock_co"))
                return int(v) if v else None
        return None

    # --- 공시 -------------------------------------------------------------

    def disclosures(self, corp_code: str, asof: datetime, days: int = 30) -> list[dict]:
        """최근 days 일 공시. 접수일이 asof 이후인 항목은 제외 (룩어헤드 방지)."""
        asof_date = asof.astimezone(UTC).date()
        bgn = asof_date - timedelta(days=days)
        data = self._get_json(
            "list.json",
            corp_code=corp_code,
            bgn_de=bgn.strftime("%Y%m%d"),
            end_de=asof_date.strftime("%Y%m%d"),
            page_count="100",
        )
        items: list[dict] = []
        for row in data.get("list") or []:
            raw = str(row.get("rcept_dt", ""))
            if len(raw) != 8:
                continue
            rcept = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            if rcept > asof_date:
                continue
            rcept_no = row.get("rcept_no", "")
            items.append(
                {
                    "title": str(row.get("report_nm", "")).strip(),
                    "rcept_dt": rcept.isoformat(),
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                }
            )
        items.sort(key=lambda i: i["rcept_dt"], reverse=True)
        return items


# --- 비율 (DART·EDGAR 공유) -------------------------------------------------


def _ratio(num: Decimal | None, den: Decimal | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return float(num / den)


def basic_ratios(
    fin: dict[str, Decimal], *, shares: int | None, price: Decimal | None
) -> dict[str, float | None]:
    """공통 키 재무제표와 주식수·가격으로 7개 비율을 계산한다. 계산 불가 항목은 None."""
    rev, rev_prev = fin.get("매출액"), fin.get("매출액_전기")
    growth = None
    if rev is not None and rev_prev is not None and rev_prev != 0:
        growth = float((rev - rev_prev) / rev_prev)
    mcap = Decimal(shares) * price if shares and price is not None else None
    return {
        "매출성장률": growth,
        "영업이익률": _ratio(fin.get("영업이익"), rev),
        "ROE": _ratio(fin.get("당기순이익"), fin.get("자본총계")),
        "부채비율": _ratio(fin.get("부채총계"), fin.get("자본총계")),
        "PER": _ratio(mcap, fin.get("당기순이익")),
        "PBR": _ratio(mcap, fin.get("자본총계")),
        "PSR": _ratio(mcap, rev),
    }


def ratios_kr(
    dart: DartClient, ticker: str, asof: date, price: Decimal | None
) -> dict[str, float | None]:
    """국내 종목 재무비율. 회사를 못 찾거나 재무제표가 없으면 전부 None."""
    code = dart.corp_code(ticker)
    if not code:
        return dict.fromkeys(RATIO_KEYS)
    year = fiscal_year_for(asof)
    fin = dart.financials(code, year)
    shares = dart.shares(code, year) if fin else None
    return basic_ratios(fin, shares=shares, price=price)
