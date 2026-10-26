"""Phase 25 단계 7: 세금 로트(FIFO)·US 장단기·워시세일 표시."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.ledger import lots
from trader.ledger.lots import FillRow

T0 = datetime(2025, 1, 2, 15, 0, tzinfo=UTC)


def f(side, qty, px, days, t="AAPL"):
    return FillRow(t, side, qty, Decimal(px), T0 + timedelta(days=days))


def test_fifo_partial_sells():
    out = lots.fifo(
        [f("BUY", 10, "100", 0), f("BUY", 10, "120", 1), f("SELL", 15, "130", 5)], market="KR"
    )
    r = out["realized"]
    assert [(x.qty, x.cost, x.pnl) for x in r] == [
        (10, Decimal("1000"), Decimal("300")),
        (5, Decimal("600"), Decimal("50")),
    ]
    assert [(lot.qty, lot.price) for lot in out["open_lots"]] == [(5, Decimal("120"))]


def test_us_long_term_after_365_days():
    out = lots.fifo(
        [f("BUY", 1, "100", 0), f("BUY", 1, "100", 10), f("SELL", 2, "150", 370)], market="US"
    )
    assert [x.term for x in out["realized"]] == ["long", "short"]


def test_kr_term_none():
    out = lots.fifo([f("BUY", 1, "100", 0), f("SELL", 1, "90", 400)], market="KR")
    assert out["realized"][0].term is None


def test_wash_sale_flag_within_30_days():
    loss = [f("BUY", 1, "100", 0), f("SELL", 1, "80", 40), f("BUY", 1, "85", 55)]
    assert lots.fifo(loss, market="US")["realized"][0].wash_sale is True
    far = [f("BUY", 1, "100", 0), f("SELL", 1, "80", 40), f("BUY", 1, "85", 80)]
    assert lots.fifo(far, market="US")["realized"][0].wash_sale is False
    gain = [f("BUY", 1, "100", 0), f("SELL", 1, "120", 40), f("BUY", 1, "85", 45)]
    assert lots.fifo(gain, market="US")["realized"][0].wash_sale is False


def test_unmatched_sell_counted():
    out = lots.fifo([f("SELL", 3, "100", 0)], market="KR")
    assert out["realized"] == [] and out["unmatched_sell_qty"] == 3


def test_report_from_ledger_and_csv(client, conn):
    for side, qty, px, days in [("BUY", 2, "100", 0), ("SELL", 2, "90", 10), ("BUY", 1, "95", 20)]:
        ts = (T0 + timedelta(days=days)).isoformat()
        cur = conn.execute(
            "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
            "created_at) VALUES ('paper-main','AAPL','US',?,?,'LIMIT','filled','auto',?)",
            (side, qty, ts),
        )
        conn.execute(
            "INSERT INTO fills(order_id, account_id, qty, price, filled_at) VALUES (?,?,?,?,?)",
            (cur.lastrowid, "paper-main", qty, px, ts),
        )
    conn.commit()
    r = client.get("/api/tax-lots", params={"account_id": "paper-main", "market": "US"}).json()
    assert r["summary"] == {
        "realized_pnl": "-20",
        "short_pnl": "-20",
        "long_pnl": "0",
        "wash_sales": 1,
        "unmatched_sell_qty": 0,
    }
    assert r["realized"][0]["wash_sale"] is True and len(r["open_lots"]) == 1
    csv = client.get(
        "/api/tax-lots", params={"account_id": "paper-main", "market": "US", "format": "csv"}
    )
    assert csv.status_code == 200 and "wash_sale" in csv.text.splitlines()[0]
    assert client.get("/api/tax-lots", params={"market": "US"}).status_code == 400
