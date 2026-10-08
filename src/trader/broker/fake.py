"""테스트·dry-run 용 가짜 브로커/시세. KisMarket·KisBroker 와 같은 메서드를 가진다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from trader.broker.errors import KisApiError
from trader.broker.models import Candle, Cash, Market, OrderRequest, OrderResult, Position, Quote
from trader.config import AccountConfig


class FakeMarket:
    def __init__(
        self,
        prices: dict[str, Decimal] | None = None,
        candles: dict[str, list[Candle]] | None = None,
        holidays: set[date] | None = None,
        *,
        fail: set[str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.prices = prices or {}
        self.candles = candles or {}
        self.holidays = holidays or set()
        self.fail = fail or set()
        self.calls: list[str] = []
        self._now = now

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise KisApiError("FAKE", f"가짜 실패 주입: {name}")

    def _quote(self, ticker: str, market: Market) -> Quote:
        if ticker not in self.prices:
            raise KisApiError("FAKE", f"가격 없음: {ticker}")
        return Quote(
            ticker=ticker,
            market=market,
            price=self.prices[ticker],
            change_pct=0.0,
            volume=0,
            asof=self._now(),
        )

    def get_price_kr(self, ticker: str) -> Quote:
        self._record("get_price_kr")
        return self._quote(ticker, "KR")

    def get_price_us(self, symbol: str, exchange: str) -> Quote:
        self._record("get_price_us")
        return self._quote(symbol, "US")

    def get_daily_kr(self, ticker: str, days: int = 250) -> list[Candle]:
        self._record("get_daily_kr")
        return list(self.candles.get(ticker, []))[-days:]

    def get_daily_us(self, symbol: str, exchange: str, days: int = 250) -> list[Candle]:
        self._record("get_daily_us")
        return list(self.candles.get(symbol, []))[-days:]

    def is_holiday(self, d: date) -> bool:
        self._record("is_holiday")
        return d in self.holidays


class FakeBroker:
    def __init__(
        self,
        account: AccountConfig,
        *,
        positions: list[Position] | None = None,
        cash: dict[Market, Cash] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.account = account
        self.positions = positions or []
        self.cash = cash or {}
        self.fail = fail or set()
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise KisApiError("FAKE", f"가짜 실패 주입: {name}")

    def get_positions(self, market: Market) -> list[Position]:
        self._record("get_positions")
        return [p for p in self.positions if p.market == market]

    def get_cash(self, market: Market) -> Cash:
        self._record("get_cash")
        if market not in self.cash:
            raise KisApiError("FAKE", f"현금 정보 없음: {market}")
        return self.cash[market]

    def place_order(self, request: OrderRequest | None) -> OrderResult:
        raise NotImplementedError("주문은 Phase 4 에서 구현합니다")

    def cancel_order(self, order_no: str) -> OrderResult:
        raise NotImplementedError("주문 취소는 Phase 4 에서 구현합니다")
