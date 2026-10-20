"""시세·일봉·휴장일 조회. 실전 env 클라이언트만 허용한다 (모의 서버는 시세류 미지원).

일봉 목록은 항상 날짜 오름차순(마지막이 최신)이다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.broker._parse import dec as _dec
from trader.broker._parse import rows as _rows
from trader.broker._parse import to_int as _int
from trader.broker.errors import KisApiError
from trader.broker.kis_client import KisClient
from trader.broker.models import Candle, InvestorFlow, OrderBook, Quote
from trader.broker.tr_ids import path
from trader.config import KST
from trader.platform import paths

logger = logging.getLogger("trader.kis")

PAGE_SIZE = 100
MAX_PAGES = 10  # 일봉 상한 약 1,000봉
HOLIDAY_CACHE_DAYS = 7


def _ymd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


class KisMarket:
    def __init__(
        self,
        client: KisClient,
        *,
        state: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if client.env != "real":
            raise ValueError(
                "시세 조회는 실전 env 클라이언트만 허용합니다 (모의 서버는 시세 미지원)"
            )
        self._client = client
        self._state = state
        self._now = now

    # --- 현재가 -------------------------------------------------------------

    def get_price_kr(self, ticker: str) -> Quote:
        data = self._client.get(
            path("DOM_PRICE"),
            "DOM_PRICE",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        out = data.get("output") or {}
        return Quote(
            ticker=ticker,
            market="KR",
            price=_dec(out.get("stck_prpr")),
            change_pct=float(_dec(out.get("prdy_ctrt"))) / 100,
            volume=_int(out.get("acml_vol")),
            asof=self._now(),
        )

    def get_industry_kr(self, ticker: str) -> str | None:
        """KRX 업종명 (현재가 응답 `bstp_kor_isnm`). 섹터 제안(`universe sectors`)용, 없으면 None.

        시세 호출과 같은 API 라 한 종목에 두 번 부르지 않도록 섹터 제안에서만 쓴다."""
        data = self._client.get(
            path("DOM_PRICE"),
            "DOM_PRICE",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        name = str((data.get("output") or {}).get("bstp_kor_isnm") or "").strip()
        return name or None

    def get_industry_us(self, symbol: str, exchange: str) -> str | None:
        """KIS 해외 업종명 (현재가상세 `e_icod`, 한글 업종명으로 온다 — 실기 2026-10-20).

        없으면 None."""
        data = self._client.get(
            path("OVS_PRICE_DETAIL"),
            "OVS_PRICE_DETAIL",
            {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        code = str((data.get("output") or {}).get("e_icod") or "").strip()
        return code or None

    def get_price_us(self, symbol: str, exchange: str) -> Quote:
        data = self._client.get(
            path("OVS_PRICE"),
            "OVS_PRICE",
            {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        out = data.get("output") or {}
        return Quote(
            ticker=symbol,
            market="US",
            price=_dec(out.get("last")),
            change_pct=float(_dec(out.get("rate"))) / 100,
            volume=_int(out.get("tvol")),
            asof=self._now(),
        )

    def get_index_kr(self, code: str = "0001") -> Quote:
        """업종 지수 (0001 KOSPI, 1001 KOSDAQ, 2001 KOSPI200)."""
        data = self._client.get(
            path("DOM_INDEX"),
            "DOM_INDEX",
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
        )
        out = data.get("output") or {}
        return Quote(
            ticker=code,
            market="KR",
            price=_dec(out.get("bstp_nmix_prpr")),
            change_pct=float(_dec(out.get("bstp_nmix_prdy_ctrt"))) / 100,
            volume=_int(out.get("acml_vol")),
            asof=self._now(),
        )

    def get_name_kr(self, ticker: str) -> str | None:
        """종목명 (일봉 API output1.hts_kor_isnm). DART 가 죽었을 때의 폴백."""
        today = self._now().astimezone(KST).date()
        data = self._client.get(
            path("DOM_DAILY"),
            "DOM_DAILY",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": (today - timedelta(days=7)).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        name = str((data.get("output1") or {}).get("hts_kor_isnm", "")).strip()
        return name or None

    # --- 분봉 -----------------------------------------------------------------

    MINUTE_PAGE = 30  # KIS 국내 분봉은 기준 시각부터 과거 30건만 준다
    MINUTE_OPEN = "090000"
    MINUTE_MAX_PAGES = 20  # 정규장 390분 = 13페이지. 비정상 응답이 와도 호출 수는 여기서 멈춘다

    def get_minute_kr(self, ticker: str, *, count: int = 30) -> list[Candle]:
        """당일 분봉. KIS 는 기준 시각부터 과거 30건만 주므로 count 만큼 1분 전 시각으로
        역방향 페이징한다(새 봉 없음·30 미만·09:00 도달·페이지 상한이면 중단). 시간 오름차순.

        체결 시각이 빈 행은 버리고, 개장 전 시각으로 내려가 전일 봉이 섞여 오면 첫 페이지의
        날짜(당일)만 남기고 멈춘다.
        """
        hour = self._now().astimezone(KST).strftime("%H%M%S")
        seen: set[str] = set()
        candles: list[Candle] = []
        day: date | None = None
        for _ in range(self.MINUTE_MAX_PAGES):
            if len(candles) >= count:
                break
            data = self._client.get(
                path("DOM_MINUTE"),
                "DOM_MINUTE",
                {
                    "FID_ETC_CLS_CODE": "",
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_HOUR_1": hour,
                    "FID_PW_DATA_INCU_YN": "N",
                },
            )
            raw_rows = _rows(data.get("output2"))
            page = [
                Candle(
                    date=_ymd(str(r["stck_bsop_date"])),
                    open=_dec(r.get("stck_oprc")),
                    high=_dec(r.get("stck_hgpr")),
                    low=_dec(r.get("stck_lwpr")),
                    close=_dec(r.get("stck_prpr")),
                    volume=_int(r.get("cntg_vol")),
                    time=str(r["stck_cntg_hour"]),
                )
                for r in raw_rows
                if r.get("stck_bsop_date") and r.get("stck_cntg_hour")
            ]
            if not page:
                break
            if day is None:
                day = max(c.date for c in page)
            today = [c for c in page if c.date == day]
            fresh = [c for c in today if c.time not in seen]
            seen.update(c.time for c in fresh)
            candles.extend(fresh)
            # 짧은 페이지 판단은 빈 시각 행을 버리기 전 원시 행 수로 (한 행이 비어도 계속 넘긴다)
            if not fresh or len(raw_rows) < self.MINUTE_PAGE or len(today) < len(page):
                break  # 전일 봉이 섞였거나 당일 봉이 바닥났다
            earliest = min(c.time for c in today)
            if earliest <= self.MINUTE_OPEN:
                break
            t = datetime.strptime(earliest, "%H%M%S") - timedelta(minutes=1)
            hour = t.strftime("%H%M%S")
        ordered = sorted(candles, key=lambda c: c.time)
        return ordered[-count:]

    def get_minute_us(self, symbol: str, exchange: str, *, count: int = 120) -> list[Candle]:
        data = self._client.get(
            path("OVS_MINUTE"),
            "OVS_MINUTE",
            {
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
                "NMIN": "1",
                "PINC": "1",
                "NEXT": "",
                "NREC": str(count),
                "FILL": "",
                "KEYB": "",
            },
        )
        candles = [
            Candle(
                date=_ymd(str(r["xymd"])),
                open=_dec(r.get("open")),
                high=_dec(r.get("high")),
                low=_dec(r.get("low")),
                close=_dec(r.get("last")),
                volume=_int(r.get("evol")),
                time=str(r.get("xhms", "")),
            )
            for r in _rows(data.get("output2"))
            if r.get("xymd")
        ]
        return sorted(candles, key=lambda c: (c.date, c.time or ""))

    # --- 호가 -----------------------------------------------------------------

    @staticmethod
    def _levels(out: dict, price_key: str, qty_key: str) -> list[tuple[Decimal, int]]:
        levels: list[tuple[Decimal, int]] = []
        for i in range(1, 11):
            price = _dec(out.get(f"{price_key}{i}"))
            qty = _int(out.get(f"{qty_key}{i}"))
            if price <= 0 and qty <= 0:
                continue
            levels.append((price, qty))
        return levels

    def get_orderbook_kr(self, ticker: str) -> OrderBook:
        data = self._client.get(
            path("DOM_ORDERBOOK"),
            "DOM_ORDERBOOK",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        out = data.get("output1") or {}
        return OrderBook(
            ticker=ticker,
            market="KR",
            asks=self._levels(out, "askp", "askp_rsqn"),
            bids=self._levels(out, "bidp", "bidp_rsqn"),
            asof=self._now(),
        )

    def get_orderbook_us(self, symbol: str, exchange: str) -> OrderBook:
        data = self._client.get(
            path("OVS_ORDERBOOK"),
            "OVS_ORDERBOOK",
            {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
        )
        out = data.get("output2") or {}
        return OrderBook(
            ticker=symbol,
            market="US",
            asks=self._levels(out, "pask", "vask"),
            bids=self._levels(out, "pbid", "vbid"),
            asof=self._now(),
        )

    # --- 투자자동향 -------------------------------------------------------------

    def get_investor_kr(self, ticker: str, days: int = 20) -> list[InvestorFlow]:
        """투자자별 일별 순매수 (KIS 는 최근 30영업일, 최신→과거). 날짜 오름차순."""
        data = self._client.get(
            path("DOM_INVESTOR"),
            "DOM_INVESTOR",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        flows = [
            InvestorFlow(
                date=_ymd(str(r["stck_bsop_date"])),
                foreign_net=_int(r.get("frgn_ntby_qty")),
                institution_net=_int(r.get("orgn_ntby_qty")),
                individual_net=_int(r.get("prsn_ntby_qty")),
            )
            for r in _rows(data.get("output"))
            if r.get("stck_bsop_date")
        ]
        flows.sort(key=lambda f: f.date)
        return flows[-days:] if days > 0 else flows

    # --- 일봉 -----------------------------------------------------------------

    def get_daily_kr(self, ticker: str, days: int = 250) -> list[Candle]:
        """KIS 는 호출당 최신 100봉(최신→과거)을 주므로 종료일을 뒤로 밀며 페이징한다."""
        by_date: dict[date, Candle] = {}
        end = self._now().astimezone(KST).date()
        for _ in range(MAX_PAGES):
            if len(by_date) >= days:
                break
            start = end - timedelta(days=int(PAGE_SIZE * 1.6))
            data = self._client.get(
                path("DOM_DAILY"),
                "DOM_DAILY",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
            )
            rows = _rows(data.get("output2"))
            if not rows:
                break
            oldest: date | None = None
            for r in rows:
                if not r.get("stck_bsop_date"):  # 상장 초기 종목은 뒤를 빈 행으로 채워 온다
                    continue
                d = _ymd(str(r["stck_bsop_date"]))
                oldest = d if oldest is None or d < oldest else oldest
                by_date.setdefault(
                    d,
                    Candle(
                        date=d,
                        open=_dec(r.get("stck_oprc")),
                        high=_dec(r.get("stck_hgpr")),
                        low=_dec(r.get("stck_lwpr")),
                        close=_dec(r.get("stck_clpr")),
                        volume=_int(r.get("acml_vol")),
                    ),
                )
            if oldest is None or len(rows) < PAGE_SIZE:
                break
            end = oldest - timedelta(days=1)
        candles = [by_date[d] for d in sorted(by_date)]
        return candles[-days:] if days > 0 else candles

    def get_daily_us(self, symbol: str, exchange: str, days: int = 250) -> list[Candle]:
        by_date: dict[date, Candle] = {}
        bymd = ""
        for _ in range(MAX_PAGES):
            if len(by_date) >= days:
                break
            data = self._client.get(
                path("OVS_DAILY"),
                "OVS_DAILY",
                {
                    "AUTH": "",
                    "EXCD": exchange,
                    "SYMB": symbol,
                    "GUBN": "0",
                    "BYMD": bymd,
                    "MODP": "1",
                },
            )
            rows = _rows(data.get("output2"))
            if not rows:
                break
            oldest: date | None = None
            for r in rows:
                if not r.get("xymd"):
                    continue
                d = _ymd(str(r["xymd"]))
                oldest = d if oldest is None or d < oldest else oldest
                by_date.setdefault(
                    d,
                    Candle(
                        date=d,
                        open=_dec(r.get("open")),
                        high=_dec(r.get("high")),
                        low=_dec(r.get("low")),
                        close=_dec(r.get("clos")),
                        volume=_int(r.get("tvol")),
                    ),
                )
            if oldest is None or len(rows) < PAGE_SIZE:
                break
            bymd = (oldest - timedelta(days=1)).strftime("%Y%m%d")
        candles = [by_date[d] for d in sorted(by_date)]
        return candles[-days:] if days > 0 else candles

    # --- 휴장일 ---------------------------------------------------------------

    def _cache_dir(self) -> Path:
        base = self._state if self._state is not None else paths.state_dir()
        d = base / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _holiday_month(self, year: int, month: int) -> dict[str, bool]:
        """해당 월의 {YYYYMMDD: 개장여부}. 7일 캐시."""
        cache = self._cache_dir() / f"holiday_{year:04d}{month:02d}.json"
        now = self._now()
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(data["fetched_at"])
            if now - fetched < timedelta(days=HOLIDAY_CACHE_DAYS):
                return {k: bool(v) for k, v in data["days"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        days: dict[str, bool] = {}
        resp = self._client.get(
            path("HOLIDAY"),
            "HOLIDAY",
            {"BASS_DT": f"{year:04d}{month:02d}01", "CTX_AREA_FK": "", "CTX_AREA_NK": ""},
        )
        for r in _rows(resp.get("output")):
            days[str(r["bass_dt"])] = str(r.get("opnd_yn", "N")).upper() == "Y"
        if days:  # 빈 응답을 7일 캐시하면 그 달 휴장일을 전부 개장일로 보게 된다
            cache.write_text(
                json.dumps({"fetched_at": now.isoformat(), "days": days}), encoding="utf-8"
            )
        return days

    def is_holiday(self, d: date) -> bool:
        """KIS 휴장일 조회 기준 개장일이 아니면 True. 목록에 없는 날짜는 개장일로 본다."""
        try:
            days = self._holiday_month(d.year, d.month)
        except KisApiError as e:
            logger.warning("휴장일 조회 실패, 개장일로 간주: %s", e)
            return False
        key = d.strftime("%Y%m%d")
        if key not in days:
            return False
        return not days[key]
