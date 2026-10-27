"""세금 로트·워시세일 (Phase 25 단계 7, **표시 전용** — 신고용 정답이 아니다).

체결(`fills` JOIN `orders`)을 FIFO 로 맞춰 매도마다 실현 로트를 만든다. US 는 보유 365일 초과면
장기(long), 아니면 단기(short), KR 은 구분 없음(None). 워시세일 경고는 손실 실현 매도의 앞뒤 30일
안에 같은 종목 매수가 있으면 표시한다(미국 규정의 단순화 — 부분 수량·옵션·다른 계좌는 보지 않는다).
수수료·세금은 원장에 없어 빠져 있다."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

WASH_DAYS = 30
LONG_TERM_DAYS = 365


@dataclass
class FillRow:
    ticker: str
    side: str  # BUY | SELL
    qty: int
    price: Decimal
    filled_at: datetime


@dataclass
class Lot:
    ticker: str
    qty: int
    price: Decimal
    bought_at: datetime


@dataclass
class Realized:
    ticker: str
    qty: int
    buy_date: date
    sell_date: date
    cost: Decimal
    proceeds: Decimal
    pnl: Decimal
    term: str | None
    wash_sale: bool = False


def fifo(fills: list[FillRow], *, market: str) -> dict:
    """{open_lots: [Lot], realized: [Realized]} — 시간순 FIFO. 보유보다 많이 판 수량(원장 밖 매수)은
    원가를 모르므로 실현 로트에서 뺀다(`unmatched` 로 센다)."""
    book: dict[str, list[Lot]] = {}
    realized: list[Realized] = []
    origin: list[int] = []  # realized[i] 를 만든 매수 체결의 순번(같은 날 다른 매수와 구분)
    lot_src: dict[int, int] = {}  # id(Lot) → 매수 체결 순번
    unmatched = 0
    ordered = sorted(fills, key=lambda x: x.filled_at)
    for i, f in enumerate(ordered):
        if f.qty <= 0:
            continue
        if f.side == "BUY":
            lot = Lot(f.ticker, f.qty, f.price, f.filled_at)
            lot_src[id(lot)] = i
            book.setdefault(f.ticker, []).append(lot)
            continue
        left = f.qty
        lots = book.setdefault(f.ticker, [])
        while left > 0 and lots:
            lot = lots[0]
            take = min(left, lot.qty)
            held_days = (f.filled_at.date() - lot.bought_at.date()).days
            cost = lot.price * take
            proceeds = f.price * take
            realized.append(
                Realized(
                    ticker=f.ticker,
                    qty=take,
                    buy_date=lot.bought_at.date(),
                    sell_date=f.filled_at.date(),
                    cost=cost,
                    proceeds=proceeds,
                    pnl=proceeds - cost,
                    term=None
                    if market != "US"
                    else ("long" if held_days > LONG_TERM_DAYS else "short"),
                )
            )
            origin.append(lot_src[id(lot)])
            lot.qty -= take
            left -= take
            if lot.qty == 0:
                lots.pop(0)
        unmatched += left
    buys = [(i, f) for i, f in enumerate(ordered) if f.side == "BUY" and f.qty > 0]
    for r, src in zip(realized, origin, strict=True):
        if r.pnl >= 0:
            continue
        lo, hi = r.sell_date - timedelta(days=WASH_DAYS), r.sell_date + timedelta(days=WASH_DAYS)
        r.wash_sale = any(
            b.ticker == r.ticker
            and lo <= b.filled_at.date() <= hi
            and i != src  # 이 로트를 만든 매수 체결 자신만 제외(같은 날 다른 매수는 대체 매수)
            for i, b in buys
        )
    open_lots = [lot for lots in book.values() for lot in lots if lot.qty > 0]
    return {"open_lots": open_lots, "realized": realized, "unmatched_sell_qty": unmatched}


def load_fills(conn: sqlite3.Connection, account_id: str, market: str) -> list[FillRow]:
    rows = conn.execute(
        "SELECT o.ticker, o.side, f.qty, f.price, f.filled_at FROM fills f "
        "JOIN orders o ON o.id = f.order_id WHERE f.account_id=? AND o.market=? "
        "AND o.asset='stock' ORDER BY f.filled_at, f.id",  # 파생 체결은 로트 밖(Phase 26)
        (account_id, market),
    ).fetchall()
    return [
        FillRow(
            ticker=r["ticker"],
            side=r["side"],
            qty=int(r["qty"]),
            price=Decimal(str(r["price"])),
            filled_at=datetime.fromisoformat(r["filled_at"]),
        )
        for r in rows
    ]


def report(
    conn: sqlite3.Connection, account_id: str, market: str, *, year: int | None = None
) -> dict:
    """API·CSV 용 직렬화. year 를 주면 그 해에 판 실현 로트만(보유 로트는 전부)."""
    out = fifo(load_fills(conn, account_id, market), market=market)
    realized = [r for r in out["realized"] if year is None or r.sell_date.year == year]

    def ser(d: dict) -> dict:
        return {
            k: (str(v) if isinstance(v, Decimal | date | datetime) else v) for k, v in d.items()
        }

    total = sum((r.pnl for r in realized), Decimal(0))
    return {
        "account_id": account_id,
        "market": market,
        "year": year,
        "realized": [ser(asdict(r)) for r in realized],
        "open_lots": [ser(asdict(lot)) for lot in out["open_lots"]],
        "summary": {
            "realized_pnl": str(total),
            "short_pnl": str(sum((r.pnl for r in realized if r.term == "short"), Decimal(0))),
            "long_pnl": str(sum((r.pnl for r in realized if r.term == "long"), Decimal(0))),
            "wash_sales": sum(1 for r in realized if r.wash_sale),
            "unmatched_sell_qty": out["unmatched_sell_qty"],
        },
        "note": "표시 전용 — 수수료·세금·환율·다른 계좌는 빠져 있어 신고용이 아니다",
    }
