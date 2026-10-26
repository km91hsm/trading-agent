"""KIS 실시간 프레임 합성(테스트 전용, 실제 수신 원문 아님)."""

from trader.broker import kis_ws


def trade_values(ticker, price, sign="2", change_pct="1.25"):
    """H0STCNT0 한 건 — 앞쪽 칸만 의미 있는 합성 값."""
    head = [ticker, "093015", str(price), sign, "100", change_pct]
    return head + ["0"] * (kis_ws.TRADE_FIELD_COUNT - len(head))


def frame(*records):
    body = "^".join("^".join(r) for r in records)
    return f"0|H0STCNT0|{len(records):03d}|{body}"
