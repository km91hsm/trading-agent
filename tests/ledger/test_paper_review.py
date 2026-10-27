"""모의 체결 대 가정 주간 점검(Stock docs/07 4-7): 결정 주문의 체결률과 지정가 대비 체결가 차이를
비용 가정과 비교하고, 기준을 넘으면 경고 문구를 낸다."""

from datetime import date

from conftest import make_account

from trader.ledger import paper_review

PAPER = make_account("paper-main", "paper", "auto", ["KR"])
REAL = make_account("real-main", "real", "off", ["KR"])


def _order(
    conn,
    side,
    price,
    status,
    fills=(),
    *,
    acct="paper-main",
    decision=True,
    at="2026-10-26T01:00:00+00:00",
    did=None,
):
    if decision and did is None:
        conn.execute(
            "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, json,"
            " created_at) VALUES ('2026-10-26','KR',?, 'BUY', 0.1, 0.7, '{}', ?)",
            (f"T{conn.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]:05d}", at),
        )
        did = conn.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, price,"
        " status, mode, created_at) VALUES (?,?, '005930','KR',?,10,'limit',?,?, 'auto', ?)",
        (did, acct, side, str(price), status, at),
    )
    for qty, px in fills:
        conn.execute(
            "INSERT INTO fills(order_id, account_id, qty, price, filled_at) VALUES (?,?,?,?,?)",
            (cur.lastrowid, acct, qty, str(px), at),
        )
    conn.commit()


def test_fill_rate_and_slippage_vs_assumption(conn):
    _order(conn, "BUY", 10000, "filled", [(10, 10000)])
    _order(conn, "BUY", 10000, "filled", [(5, 10010), (5, 10030)])  # 지정가보다 20bp 비쌈
    _order(conn, "SELL", 10000, "filled", [(10, 9990)])  # 매도 10bp 불리
    _order(conn, "BUY", 10000, "expired")
    _order(conn, "BUY", 10000, "cancelled")
    _order(conn, "BUY", 10000, "accepted")  # 아직 살아 있음 → 체결률 분모에서 뺀다
    _order(conn, "BUY", 10000, "failed")  # 거래소에 안 나감 → 제외
    _order(conn, "BUY", 10000, "filled", [(10, 10000)], decision=False)  # 단타·외부 → 제외
    _order(conn, "BUY", 10000, "filled", [(10, 10000)], acct="real-main")  # 실전 → 제외
    r = paper_review.compute(
        conn, [PAPER, REAL], since=date(2026, 10, 20), until=date(2026, 10, 27)
    )
    (row,) = r["items"]
    assert row["account_id"] == "paper-main" and row["market"] == "KR"
    assert row["terminal"] == 5 and row["filled"] == 3 and row["open"] == 1
    assert abs(row["fill_rate"] - 0.6) < 1e-9
    assert abs(row["slippage_bp"] - 10.0) < 1e-6  # (0 + 20 + 10) / 3
    assert row["assumed_bp"] == 15  # KR 왕복 30bp 의 편도
    assert any("체결률" in a for a in r["alerts"])  # 60% < 80%


def test_quiet_when_too_few_orders(conn):
    _order(conn, "BUY", 10000, "expired")
    r = paper_review.compute(conn, [PAPER], since=date(2026, 10, 20), until=date(2026, 10, 27))
    assert r["alerts"] == [] and r["items"][0]["terminal"] == 1


def test_slippage_alert_over_twice_assumption(conn):
    for _ in range(5):
        _order(conn, "BUY", 10000, "filled", [(10, 10040)])  # 40bp > 2×15
    r = paper_review.compute(conn, [PAPER], since=date(2026, 10, 20), until=date(2026, 10, 27))
    assert any("슬리피지" in a for a in r["alerts"])
    text = paper_review.message(r)
    assert text.startswith("[모의 체결 점검]") and "paper-main KR" in text


def test_partial_is_in_progress_and_qty_rate(conn):
    for _ in range(5):
        _order(conn, "BUY", 10000, "partial", [(1, 10000)])
    _order(conn, "BUY", 10000, "filled", [(10, 10000)])
    _order(conn, "BUY", 10000, "expired", [(4, 10000)])  # 마감에 잔량 소멸
    r = paper_review.compute(conn, [PAPER], since=date(2026, 10, 20), until=date(2026, 10, 27))
    (row,) = r["items"]
    assert row["open"] == 5 and row["terminal"] == 2
    assert abs(row["qty_fill_rate"] - 14 / 20) < 1e-9


def test_reprice_chain_counts_once_with_slippage_vs_original_limit(conn):
    """2-L12: 재호가가 원 주문을 cancelled(체결 0)로 남겨도 결정 단위 주문 사슬 하나로 세고,
    슬리피지는 원 지정가(백테스트가 가정한 체결가) 대비 실제 체결가로 잰다."""
    _order(conn, "BUY", 10000, "cancelled")  # 원 지정가 — 재호가로 취소
    did = conn.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    _order(conn, "BUY", 10050, "filled", [(10, 10050)], did=did)  # 현재가 지정가 재제출·체결
    for _ in range(4):
        _order(conn, "BUY", 10000, "filled", [(10, 10000)])
    r = paper_review.compute(conn, [PAPER], since=date(2026, 10, 20), until=date(2026, 10, 27))
    (row,) = r["items"]
    assert row["terminal"] == 5 and row["filled"] == 5 and abs(row["fill_rate"] - 1.0) < 1e-9
    assert abs(row["slippage_bp"] - 10.0) < 1e-6  # (50 + 0 × 4) / 5
    assert not any("체결률" in a for a in r["alerts"])
