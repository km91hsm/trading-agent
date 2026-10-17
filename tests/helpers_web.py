"""웹 API 테스트 헬퍼: 가짜 브로커·시세·토큰으로 WebDeps 를 만든다."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Candle, Cash
from trader.config import AccountConfig, Settings
from trader.ledger import db
from trader.web.app import WebDeps

TOKEN = "T0KEN"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)
D = date(2026, 10, 9)


class Sent:
    def __init__(self):
        self.msgs: list[str] = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def _accounts() -> list[AccountConfig]:
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]


def _brokers(accounts):
    cash = {
        "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
        "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
    }
    return {a.id: FakeBroker(a, cash=cash) for a in accounts}


def _candles(n=30) -> list[Candle]:
    out = []
    for i in range(n):
        d = date(2026, 8, 20) + timedelta(days=i)
        px = Decimal(70000 + i * 100)
        out.append(
            Candle(date=d, open=px, high=px + 500, low=px - 500, close=px + 100, volume=1000)
        )
    return out


def _minutes(n=5, d=date(2026, 10, 9), start_hhmm="0900") -> list[Candle]:
    """현지 start_hhmm 부터 1분 간격 분봉 n 개 (time="HHMM00")."""
    base = datetime.strptime(start_hhmm, "%H%M")
    out = []
    for i in range(n):
        px = Decimal(70000 + i)
        hhmm = (base + timedelta(minutes=i)).strftime("%H%M")
        out.append(
            Candle(
                date=d, open=px, high=px + 5, low=px - 5, close=px + 1, volume=10, time=f"{hhmm}00"
            )
        )
    return out


def make_deps(project, *, token: str | None = TOKEN, dist=None, market=True) -> WebDeps:
    accounts = _accounts()
    return WebDeps(
        settings=Settings(),
        accounts=accounts,
        conn_factory=lambda: db.connect(),
        brokers=_brokers(accounts),
        market=FakeMarket(
            prices={"005930": Decimal("71500"), "AAPL": Decimal("227.55")},
            candles={"005930": _candles(), "AAPL": _candles()},
            minutes={"005930": _minutes(), "AAPL": _minutes()},
        )
        if market
        else None,
        state=None,
        token=token,
        dist=dist,
        notifier=Sent(),
        now=lambda: NOW,
    )
