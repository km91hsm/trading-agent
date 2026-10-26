"""장 마감 일일 리포트(Stock D-095): 계좌·시장별 NAV·전일 대비·최근 5일 실현손익·주문/체결·미결·
킬스위치 한 줄. 오늘 스냅샷이 없는 계좌·시장은 뺀다."""

from datetime import date

from conftest import make_account

from trader.ledger import daily_report
from trader.risk import killswitch

PAPER = make_account("paper-main", "paper", "auto", ["KR", "US"])
D = date(2026, 10, 27)


def _snap(conn, d, nav, ticker="", qty=0, market="KR"):
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES ('paper-main',?,?,?,?,'0','0',"
        "'0',?,'0','2026-10-27T07:00:00+00:00')",
        (market, d, ticker, qty, str(nav)),
    )
    conn.commit()


def _order(conn, status, at="2026-10-27T01:00:00+00:00"):
    conn.execute(
        "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
        "created_at) VALUES ('paper-main','005930','KR','BUY',1,'limit',?,'auto',?)",
        (status, at),
    )
    conn.commit()


def test_report_rows_and_message(conn):
    _snap(conn, "2026-10-24", 10_000_000)
    _snap(conn, "2026-10-27", 10_150_000, "005930", 3)
    _snap(conn, "2026-10-27", 10_150_000, "000660", 1)
    _order(conn, "filled")
    _order(conn, "accepted")
    _order(conn, "accepted", at="2026-10-20T01:00:00+00:00")  # 지난 주문은 오늘 집계 밖
    r = daily_report.compute(conn, [PAPER], markets=["KR", "US"], today_of=lambda m: D)
    (row,) = r["items"]  # US 는 오늘 스냅샷이 없어 빠진다
    assert row["market"] == "KR" and row["holdings"] == 2
    assert abs(row["change_pct"] - 0.015) < 1e-9
    assert row["orders"] == {"filled": 1, "accepted": 1} and row["open"] == 2
    text = daily_report.message(r)
    assert text.startswith("[일일 리포트]") and "paper-main KR" in text and "+1.50%" in text
    killswitch.engage(conn, reason="테스트", source="cli")
    assert "킬스위치" in daily_report.message(
        daily_report.compute(conn, [PAPER], markets=["KR"], today_of=lambda m: D)
    )


def test_empty_when_no_snapshots(conn):
    r = daily_report.compute(conn, [PAPER], markets=["KR"], today_of=lambda m: D)
    assert r["items"] == []
