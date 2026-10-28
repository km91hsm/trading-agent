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
from trader.broker._parse import first_of as _first
from trader.broker._parse import rows as _rows
from trader.broker._parse import to_int as _int
from trader.broker.errors import KisApiError
from trader.broker.kis_client import KisClient
from trader.broker.models import Candle, FoKind, FoQuote, InvestorFlow, OrderBook, Quote, RankRow
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

    @property
    def client(self):
        return self._client

    # --- 실시간 웹소켓 접속키 (화면 시세 허브 `web/live_quotes`) ----------------

    def approval_key(self) -> str:
        return self._client.auth.approval_key()

    def invalidate_approval(self) -> None:
        self._client.auth.invalidate_approval()

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
            status_code=str(out.get("iscd_stat_cls_code") or "").strip() or None,
            warn_code=str(out.get("mrkt_warn_cls_code") or "").strip() or None,
            halted=(str(out.get("temp_stop_yn") or "").strip().upper() == "Y")
            if out.get("temp_stop_yn") is not None
            else None,
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

    # --- 예탁원 권리 일정 (Stock D-108·D-113, 블랙아웃용) ------------------------

    _KSD = {
        "paidin": ("KSD_PAIDIN", {"GB1": "1"}),
        "bonus": ("KSD_BONUS", {}),
        "merger_split": ("KSD_MERGER_SPLIT", {}),
        "rev_split": ("KSD_REV_SPLIT", {"MARKET_GB": "0"}),
    }

    def ksd_schedule(self, kind: str, start: date, end: date) -> list[dict]:
        """기준일이 start~end 인 권리 일정 [{ticker, kind, date, name}] (연속조회 CTS)."""
        name, extra = self._KSD[kind]
        data = self._client.get_pages(
            path(name),
            name,
            {
                "CTS": "",
                "F_DT": start.strftime("%Y%m%d"),
                "T_DT": end.strftime("%Y%m%d"),
                "SHT_CD": "",
                **extra,
            },
            ctx_keys=("CTS", "CTS"),
            output_key="output1",
        )
        out = []
        for r in _rows(data.get("output1") or data.get("output")):
            raw, sym = str(r.get("record_date") or "").strip(), str(r.get("sht_cd") or "").strip()
            if len(raw) != 8 or not raw.isdigit() or not sym:
                continue
            out.append(
                {
                    "ticker": sym,
                    "kind": kind,
                    "date": _ymd(raw).isoformat(),
                    "name": str(r.get("isin_name") or "").strip() or None,
                }
            )
        return out

    # --- 순위 (Phase 25 테마 열기, 응답 필드명 미확인 → 관대한 파서) -----------------

    _RANK_PARAMS = {
        "VOLUME_RANK": {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        },
        "FLUCT_RANK": {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": "0000",
            "fid_rank_sort_cls_code": "0",
            "fid_input_cnt_1": "0",
            "fid_prc_cls_code": "0",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "",
            "fid_rsfl_rate2": "",
        },
        "FRGN_INST_TOTAL": {
            "FID_COND_MRKT_DIV_CODE": "V",
            "FID_COND_SCR_DIV_CODE": "16449",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_ETC_CLS_CODE": "0",
        },
    }

    def _rank(self, name: str, limit: int) -> list[RankRow]:
        data = self._client.get(path(name), name, dict(self._RANK_PARAMS[name]))
        out: list[RankRow] = []
        for i, r in enumerate(_rows(data.get("output") or data.get("output1")), start=1):
            code = _first(r, "mksc_shrn_iscd", "stck_shrn_iscd", "pdno", "iscd")
            if not code or not str(code).strip().isalnum():
                continue
            pct = _first(r, "prdy_ctrt", "prdy_vrss_rate")
            vol = _first(r, "acml_vol", "vol")
            frgn = _first(r, "frgn_ntby_qty")
            orgn = _first(r, "orgn_ntby_qty")
            rank = _first(r, "data_rank", "rank")
            out.append(
                RankRow(
                    ticker=str(code).strip(),
                    name=(str(_first(r, "hts_kor_isnm", "prdt_name") or "").strip() or None),
                    rank=_int(rank) if rank is not None else i,
                    change_pct=float(_dec(pct)) / 100 if pct is not None else None,
                    volume=_int(vol) if vol is not None else None,
                    net_buy=(_int(frgn) + _int(orgn))
                    if frgn is not None or orgn is not None
                    else None,
                )
            )
            if len(out) >= limit:
                break
        return out

    def volume_rank_kr(self, limit: int = 30) -> list[RankRow]:
        """거래량 순위 (FHPST01710000)."""
        return self._rank("VOLUME_RANK", limit)

    def fluctuation_rank_kr(self, limit: int = 30) -> list[RankRow]:
        """등락률 순위 (FHPST01700000)."""
        return self._rank("FLUCT_RANK", limit)

    def foreign_institution_kr(self, limit: int = 30) -> list[RankRow]:
        """외인·기관 순매수 합계 순위 (FHPTJ04400000). net_buy = 외인 + 기관 순매수 수량."""
        return self._rank("FRGN_INST_TOTAL", limit)

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


# --- 국내선물옵션 시세(Phase 26). 실전·모의 같은 tr_id, 시세는 실전 클라이언트만 ------------------

FO_DIV = {"FUTURES": "F", "CALL": "O", "PUT": "O"}
OPTION_CHAIN_MAX_CALLS = 18


def _hms_to_dt(hms: object, base: datetime) -> datetime | None:
    """응답의 'HHMMSS' 시각을 base(KST 날짜) 의 시각으로. 없으면 None(수신시각으로 대체하지
    않는다)."""
    s = str(hms or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    local = base.astimezone(KST).replace(
        hour=int(s[:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0
    )
    return local.astimezone(UTC)


def _opt_float(v: object) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(_dec(v))
    except Exception:  # noqa: BLE001
        return None


def _opt_dec(v: object) -> Decimal | None:
    return None if v in (None, "") else _dec(v)


def _fo_price(self: KisMarket, code: str, kind: FoKind) -> FoQuote:
    """선물옵션 시세. output1 선물/옵션 필드 + output2 기초지수(코스피200). 옵션은 그릭·IV·
    행사가."""
    data = self._client.get(
        path("FO_PRICE"),
        "FO_PRICE",
        {"FID_COND_MRKT_DIV_CODE": FO_DIV[kind], "FID_INPUT_ISCD": code},
    )
    out = data.get("output1") or {}
    idx = _kospi200_block(data)
    now = self._now()
    ltd_raw = str(out.get("futs_last_tr_date") or "").strip()
    days_raw = out.get("hts_rmnn_dynu")
    under_raw = idx.get("bstp_nmix_prpr")
    bid = _opt_dec(_first(out, "futs_bidp", "bidp1", "futs_bidp1"))
    ask = _opt_dec(_first(out, "futs_askp", "askp1", "futs_askp1"))
    asof = _hms_to_dt(_first(out, "futs_cntg_hour", "stck_cntg_hour", "bsop_hour"), now)
    if bid is None or ask is None or asof is None:
        # 실기 1(2026-10-28): 시세 응답에는 시각·1호가가 없다 → 호가 응답 output2 의 1호가와
        # 호가접수시간(시장 시각). 실패하면 None 그대로(진입 게이트가 막는다, 수신시각 대체 없음)
        book = _fo_book_top(self, code, kind, now)
        bid = bid if bid is not None else book[0]
        ask = ask if ask is not None else book[1]
        asof = asof if asof is not None else book[2]
    return FoQuote(
        code=code,
        kind=kind,
        price=_dec(out.get("futs_prpr")),
        bid=bid,
        ask=ask,
        change_pct=float(_dec(out.get("futs_prdy_ctrt"))) / 100,
        volume=_int(out.get("acml_vol")),
        open_interest=_int(out["hts_otst_stpl_qty"]) if out.get("hts_otst_stpl_qty") else None,
        basis=_opt_dec(out.get("basis")) if kind == "FUTURES" else None,
        upper=_opt_dec(out.get("futs_mxpr")),
        lower=_opt_dec(out.get("futs_llam")),
        last_trade_date=_ymd(ltd_raw) if len(ltd_raw) == 8 else None,
        days_left=_int(days_raw) if days_raw not in (None, "") else None,
        hist_vol=_opt_float(out.get("hist_vltl")),
        iv=_opt_float(out.get("hts_ints_vltl")) if kind != "FUTURES" else None,
        delta=_opt_float(out.get("delta_val")) if kind != "FUTURES" else None,
        gamma=_opt_float(out.get("gama")) if kind != "FUTURES" else None,
        theta=_opt_float(out.get("theta")) if kind != "FUTURES" else None,
        vega=_opt_float(out.get("vega")) if kind != "FUTURES" else None,
        strike=_opt_dec(out.get("acpr")) if kind != "FUTURES" else None,
        underlying=_opt_dec(under_raw),
        underlying_asof=_hms_to_dt(_first(idx, "bsop_hour", "stck_cntg_hour"), now),
        quote_asof=asof,
        received_at=now,
    )


def _kospi200_block(data: dict) -> dict:
    """시세 응답의 코스피200 묶음. 실기 1(2026-10-28): output2 는 코스피 종합(업종 0001), output3 이
    코스피200(2001) — 업종코드로 고르고, 코드가 붙은 묶음 중 2001 이 없으면 빈 묶음(기초지수
    없음 → 게이트가 막는다). 코드가 없는 옛 응답만 output2 를 그대로 쓴다."""
    blocks = [data.get(k) or {} for k in ("output3", "output2")]
    coded = [b for b in blocks if str(b.get("bstp_cls_code") or "").strip()]
    if coded:
        return next((b for b in coded if str(b["bstp_cls_code"]).strip() == "2001"), {})
    return data.get("output2") or {}


def _book_out(data: dict) -> dict:
    """호가 응답의 호가 묶음 — 실기로 확인한 output2, 없으면 옛 가정 output1."""
    out2 = data.get("output2") or {}
    return out2 if out2.get("futs_askp1") or out2.get("futs_bidp1") else (data.get("output1") or {})


def _fo_book_top(
    self: KisMarket, code: str, kind: FoKind, now: datetime
) -> tuple[Decimal | None, Decimal | None, datetime | None]:
    """(매수 1호가, 매도 1호가, 호가접수시간). 조회 실패는 (None, None, None)."""
    try:
        data = self._client.get(
            path("FO_ORDERBOOK"),
            "FO_ORDERBOOK",
            {"FID_COND_MRKT_DIV_CODE": FO_DIV[kind], "FID_INPUT_ISCD": code},
        )
    except KisApiError as e:
        logger.warning("파생 호가 조회 실패 %s(시각·1호가 없음): %s", code, e)
        return None, None, None
    b = _book_out(data)
    return (
        _opt_dec(b.get("futs_bidp1")),
        _opt_dec(b.get("futs_askp1")),
        _hms_to_dt(b.get("aspr_acpt_hour"), now),
    )


def _fo_orderbook(self: KisMarket, code: str, kind: FoKind) -> OrderBook:
    data = self._client.get(
        path("FO_ORDERBOOK"),
        "FO_ORDERBOOK",
        {"FID_COND_MRKT_DIV_CODE": FO_DIV[kind], "FID_INPUT_ISCD": code},
    )
    out = _book_out(data)
    now = self._now()
    return OrderBook(
        ticker=code,
        market="KR",
        asks=self._levels(out, "futs_askp", "askp_rsqn"),
        bids=self._levels(out, "futs_bidp", "bidp_rsqn"),
        asof=_hms_to_dt(out.get("aspr_acpt_hour"), now) or now,
    )


def _fo_daily(self: KisMarket, code: str, kind: FoKind, *, days: int = 100) -> list[Candle]:
    """일봉(날짜 오름차순). 한 호출(최근 days 거래일 창)만 — 파생은 월물 수명이 짧다."""
    end = self._now().astimezone(KST).date()
    start = end - timedelta(days=int(days * 1.6) + 7)
    data = self._client.get(
        path("FO_DAILY"),
        "FO_DAILY",
        {
            "FID_COND_MRKT_DIV_CODE": FO_DIV[kind],
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
        },
    )
    out = [
        Candle(
            date=_ymd(str(r["stck_bsop_date"])),
            open=_dec(r.get("futs_oprc")),
            high=_dec(r.get("futs_hgpr")),
            low=_dec(r.get("futs_lwpr")),
            close=_dec(r.get("futs_prpr")),
            volume=_int(r.get("acml_vol")),
        )
        for r in _rows(data.get("output2"))
        if r.get("stck_bsop_date")
    ]
    out.sort(key=lambda c: c.date)
    return out[-days:]


def _fo_minute(self: KisMarket, code: str, kind: FoKind, *, count: int = 60) -> list[Candle]:
    """당일 1분봉(시간 오름차순, 체결 시각 빈 행 제외). 한 호출(최대 102건)."""
    local = self._now().astimezone(KST)
    data = self._client.get(
        path("FO_MINUTE"),
        "FO_MINUTE",
        {
            "FID_COND_MRKT_DIV_CODE": FO_DIV[kind],
            "FID_INPUT_ISCD": code,
            "FID_HOUR_CLS_CODE": "60",
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "N",
            "FID_INPUT_DATE_1": local.strftime("%Y%m%d"),
            "FID_INPUT_HOUR_1": local.strftime("%H%M%S"),
        },
    )
    out = [
        Candle(
            date=_ymd(str(r["stck_bsop_date"])),
            open=_dec(r.get("futs_oprc")),
            high=_dec(r.get("futs_hgpr")),
            low=_dec(r.get("futs_lwpr")),
            close=_dec(r.get("futs_prpr")),
            volume=_int(r.get("cntg_vol")),
            time=str(r["stck_cntg_hour"]),
        )
        for r in _rows(data.get("output2"))
        if r.get("stck_bsop_date") and r.get("stck_cntg_hour")
    ]
    out.sort(key=lambda c: (c.date, c.time or ""))
    return out[-count:]


def _option_chain(
    self: KisMarket, contracts: list, *, strikes: list[Decimal]
) -> tuple[list[FoQuote], list[str]]:
    """행사가 목록에 드는 옵션 계약의 시세(≤ OPTION_CHAIN_MAX_CALLS 호출). 실패 행은 건너뛰고
    코드를 errors 로 돌려준다."""
    wanted = set(strikes)
    targets = [c for c in contracts if c.strike is not None and c.strike in wanted]
    targets = targets[:OPTION_CHAIN_MAX_CALLS]
    quotes: list[FoQuote] = []
    errors: list[str] = []
    for c in targets:
        try:
            quotes.append(self.get_fo_price(c.code, c.kind))
        except KisApiError as e:
            logger.warning("옵션 체인 시세 실패 %s: %s", c.code, e)
            errors.append(c.code)
    return quotes, errors


def _kospi200(self: KisMarket) -> tuple[Decimal, datetime | None]:
    """코스피200 현재값과 응답의 시장 시각(없으면 None — 수신시각으로 대체하지 않는다)."""
    data = self._client.get(
        path("DOM_INDEX"),
        "DOM_INDEX",
        {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "2001"},
    )
    out = data.get("output") or {}
    return _dec(out.get("bstp_nmix_prpr")), _hms_to_dt(
        _first(out, "bsop_hour", "stck_cntg_hour"), self._now()
    )


KisMarket.get_fo_price = _fo_price  # type: ignore[attr-defined]
KisMarket.get_fo_orderbook = _fo_orderbook  # type: ignore[attr-defined]
KisMarket.get_fo_daily = _fo_daily  # type: ignore[attr-defined]
KisMarket.get_fo_minute = _fo_minute  # type: ignore[attr-defined]
KisMarket.get_option_chain = _option_chain  # type: ignore[attr-defined]
KisMarket.get_kospi200 = _kospi200  # type: ignore[attr-defined]
