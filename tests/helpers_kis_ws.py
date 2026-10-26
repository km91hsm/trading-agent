"""KIS 실시간 프레임 합성(테스트 전용, 실제 수신 원문 아님)."""

from trader.broker import kis_ws


def trade_values(ticker, price, sign="2", change_pct="1.25"):
    """H0STCNT0 한 건 — 앞쪽 칸만 의미 있는 합성 값."""
    head = [ticker, "093015", str(price), sign, "100", change_pct]
    return head + ["0"] * (kis_ws.TRADE_FIELD_COUNT - len(head))


def frame(*records):
    body = "^".join("^".join(r) for r in records)
    return f"0|H0STCNT0|{len(records):03d}|{body}"


def index_values(code, price, sign="2", change_pct="0.50"):
    """H0UPCNT0 한 건: 업종코드·시각·현재지수·부호·대비 … 9번째 칸 등락률."""
    v = [code, "093015", price, sign, "10.5", "0", "0", "0", "0", change_pct]
    return v + ["0"] * (27 - len(v))


def book_values(ticker):
    """H0STASP0 한 건: 매도 71100~, 매수 71000~, 잔량 10씩, 총잔량 1000/2000."""
    asks = [str(71100 + i * 100) for i in range(10)]
    bids = [str(71000 - i * 100) for i in range(10)]
    qty = ["10"] * 20
    return [ticker, "093015", "0", *asks, *bids, *qty, "1000", "2000"] + ["0"] * 14
