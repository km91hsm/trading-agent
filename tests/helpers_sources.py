"""수집기 테스트 공용 헬퍼 (픽스처 로드, 가짜 클라이언트)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.broker.errors import KisApiError
from trader.broker.models import Candle, InvestorFlow, OrderBook, Quote

FIXTURES = Path(__file__).parent / "sources" / "fixtures"
T0 = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)  # 2026-10-09 15:00 KST


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def corpcode_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", load_bytes("dart_corpcode.xml"))
    return buf.getvalue()


def make_candles(n: int, start: date = date(2025, 1, 1), base: int = 70000) -> list[Candle]:
    out = []
    d = start
    i = 0
    while len(out) < n:
        if d.weekday() < 5:
            c = Decimal(base + i * 10)
            out.append(
                Candle(date=d, open=c - 100, high=c + 200, low=c - 200, close=c, volume=1000 + i)
            )
            i += 1
        d += timedelta(days=1)
    return out


class FakeMarket:
    """KisMarket 과 같은 메서드. fail_* 로 메서드별 실패를 주입한다."""

    def __init__(self, *, fail: set[str] | None = None, candles: int = 300, now=lambda: T0):
        self.fail = fail or set()
        self.calls: list[str] = []
        self._candles = make_candles(candles)
        self._now = now

    def _rec(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise KisApiError("FAKE", f"가짜 실패: {name}")

    def _quote(self, ticker: str, market: str) -> Quote:
        return Quote(
            ticker=ticker,
            market=market,
            price=Decimal("71500"),
            change_pct=0.007,
            volume=12345678,
            asof=self._now(),
        )

    def get_price_kr(self, ticker):
        self._rec("get_price_kr")
        return self._quote(ticker, "KR")

    def get_price_us(self, symbol, exchange):
        self._rec("get_price_us")
        return self._quote(symbol, "US")

    def get_index_kr(self, code="0001"):
        self._rec("get_index_kr")
        return self._quote(code, "KR")

    def get_daily_kr(self, ticker, days=250):
        self._rec("get_daily_kr")
        return self._candles[-days:]

    def get_daily_us(self, symbol, exchange, days=250):
        self._rec("get_daily_us")
        return self._candles[-days:]

    def get_minute_kr(self, ticker):
        self._rec("get_minute_kr")
        return [c.model_copy(update={"time": "153000"}) for c in self._candles[-3:]]

    def get_minute_us(self, symbol, exchange, count=120):
        self._rec("get_minute_us")
        return [c.model_copy(update={"time": "155900"}) for c in self._candles[-3:]]

    def get_orderbook_kr(self, ticker):
        self._rec("get_orderbook_kr")
        return OrderBook(
            ticker=ticker,
            market="KR",
            asks=[(Decimal(71600), 100)],
            bids=[(Decimal(71500), 110)],
            asof=self._now(),
        )

    def get_orderbook_us(self, symbol, exchange):
        self._rec("get_orderbook_us")
        return OrderBook(
            ticker=symbol,
            market="US",
            asks=[(Decimal("227.56"), 300)],
            bids=[(Decimal("227.55"), 250)],
            asof=self._now(),
        )

    def get_investor_kr(self, ticker, days=20):
        self._rec("get_investor_kr")
        return [
            InvestorFlow(date=c.date, foreign_net=100, institution_net=-50, individual_net=-50)
            for c in self._candles[-days:]
        ]

    def is_holiday(self, d):
        self._rec("is_holiday")
        return False


class FakeDart:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def corp_code(self, ticker):
        return "00126380" if ticker == "005930" else None

    def corp_name(self, ticker):
        return "삼성전자" if ticker == "005930" else None

    def financials(self, corp_code, year, reprt_code="11011"):
        if self.fail:
            raise RuntimeError("DART down")
        return {
            "매출액": Decimal(330),
            "매출액_전기": Decimal(300),
            "영업이익": Decimal(33),
            "당기순이익": Decimal(40),
            "자산총계": Decimal(500),
            "부채총계": Decimal(100),
            "자본총계": Decimal(400),
        }

    def shares(self, corp_code, year, reprt_code="11011"):
        return 100

    def financial_history(self, corp_code, asof):
        if self.fail:
            raise RuntimeError("DART down")
        fin = self.financials(corp_code, 2025)
        years = {y: dict(fin) for y in (2023, 2024, 2025)}
        return {"years": years, "latest_quarter": {"reprt_code": "11012", "year": 2026, **fin}}

    def disclosures(self, corp_code, asof, days=30):
        return [{"title": "공시", "rcept_dt": "2026-10-07", "url": "https://dart.fss.or.kr/x"}]
