from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from helpers_agents import ok_decision
from helpers_ml import trading_days

from trader.ledger import performance, repo
from trader.ml import features as feat

D0 = date(2026, 9, 1)
DAYS = trading_days(25, start=D0)


def _navs(conn, values, account="paper-main", market="KR"):
    for d, v in zip(DAYS, values, strict=False):
        repo.snapshot_positions(
            conn,
            account_id=account,
            d=d,
            market=market,
            nav=Decimal(str(v)),
            cash=Decimal(1),
            positions=[],
        )


def _store(prices: dict[str, list[float]]):
    rows = []
    for t, closes in prices.items():
        for d, c in zip(DAYS[: len(closes)], closes, strict=True):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": c, "volume": 1})
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    feat.write_rows(df[feat.FEATURE_COLUMNS])


def test_nav_series_and_metrics(conn):
    _navs(conn, [100, 102, 101, 105, 95, 98])
    s = performance.nav_series(conn, "paper-main", "KR", days=60)
    assert [v for _, v in s] == [Decimal(x) for x in (100, 102, 101, 105, 95, 98)]
    m = performance.metrics_of(s)
    assert m["return"] == pytest.approx(-0.02) and m["mdd"] == pytest.approx(-0.0952, abs=1e-3)
    assert m["days"] == 6 and m["start"] == DAYS[0].isoformat() and m["sortino"] is not None
    assert performance.metrics_of([]) == {
        "return": None,
        "mdd": None,
        "sortino": None,
        "days": 0,
        "start": None,
        "end": None,
    }
    assert (
        performance.metrics_of(
            [(DAYS[0].isoformat(), Decimal(100)), (DAYS[1].isoformat(), Decimal(101))]
        )["sortino"]
        is None
    )  # 하방 없음


def test_benchmarks_from_feature_store(project):
    _store({"A": [100] * 5 + [110] * 20, "B": [100] * 5 + [90] * 20, "C": [100] * 25})
    b = performance.benchmarks(None, "KR", DAYS[0], DAYS[10], tickers=["A", "B"])
    assert b["one_over_n"]["return"] == pytest.approx(0.0) and b["buy_and_hold"][
        "return"
    ] == pytest.approx(0.0)
    b2 = performance.benchmarks(None, "KR", DAYS[0], DAYS[10], tickers=["A"])
    assert b2["buy_and_hold"]["return"] == pytest.approx(0.10)
    assert len(b2["one_over_n"]["series"]) == 11 and b2["quant"] is None
    assert (
        performance.benchmarks(None, "US", DAYS[0], DAYS[10], tickers=["A"])["one_over_n"]["return"]
        is None
    )


def test_cost_sensitivity_and_turnover(conn):
    _navs(conn, [1_000_000] * 10)
    dec_id = repo.ingest_checkpoint(
        conn, DAYS[1], "KR", "005930", "30_decision", {"data": ok_decision()}
    )
    for n, d in enumerate(DAYS[1:4]):
        repo.insert_order(
            conn,
            decision_id=dec_id,
            account_id="paper-main",
            ticker="005930",
            market="KR",
            side="BUY",
            qty=1,
            order_type="LIMIT",
            price=Decimal("100000"),
            status="accepted",
            order_no=str(n),
            message="",
            mode="auto",
            created_at=f"{d.isoformat()}T01:00:00+00:00",
        )
    cs = performance.cost_sensitivity(conn, "paper-main", "KR", DAYS[0], DAYS[9])
    assert cs["0"] == 0.0 and cs["30"] == pytest.approx(-300_000 * 0.003 / 1_000_000)
    assert performance.turnover(conn, "paper-main", "KR", DAYS[0], DAYS[9]) == pytest.approx(0.3)
    assert performance.turnover(conn, "real-main", "KR", DAYS[0], DAYS[9]) == 0.0


def test_report_account_filter_and_empty(conn, project):
    assert performance.report(conn, None, "real-main", "KR", days=30)["metrics"]["days"] == 0
    _navs(conn, [100, 110])
    _store({"A": [100] * 25})
    r = performance.report(conn, None, "paper-main", "KR", days=30)
    assert r["metrics"]["return"] == pytest.approx(0.10) and "decision_distribution" in r
    assert r["account_id"] == "paper-main" and r["benchmarks"]["one_over_n"]["return"] is not None
    assert "cost_sensitivity" in r and "turnover" in r and r["convergence_rate"] is None


def test_one_over_n_is_universe_and_bh_is_held(project):
    _store({"A": [100] * 5 + [110] * 20, "B": [100] * 5 + [90] * 20, "C": [100] * 25})
    b = performance.benchmarks(None, "KR", DAYS[0], DAYS[10], tickers=["A"])
    assert b["one_over_n"]["return"] == pytest.approx(0.0)  # A·B·C 등가중(유니버스)
    assert b["buy_and_hold"]["return"] == pytest.approx(0.10)  # 보유 A 만
    assert b["benchmark_universe"] == ["A", "B", "C"]


# --- 이연 회수: 날짜 창·KST 주문 창 ---------------------------------------------------


def test_nav_history_is_date_window_not_count(conn):
    for d, v in [("2026-09-01", 100), ("2026-10-12", 101), ("2026-10-15", 102)]:
        repo.snapshot_positions(
            conn,
            account_id="paper-main",
            d=date.fromisoformat(d),
            market="KR",
            nav=Decimal(v),
            cash=Decimal(v),
            positions=[],
        )
    got = repo.nav_history(conn, "paper-main", "KR", days=10, today=date(2026, 10, 17))
    assert [d for d, _ in got] == ["2026-10-12", "2026-10-15"]


def _insert_order(conn, *, created_at: str, amount: str):
    conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, "
        "price, status, mode, created_at, amount) VALUES (1,'paper-main','005930','KR','BUY',1,"
        "'LIMIT','1','placed','auto',?,?)",
        (created_at, amount),
    )
    conn.commit()


def test_orders_window_uses_kst_day(conn):
    # 2026-10-17T23:30Z = 10-18 08:30 KST → 10-17 창에 들어가지 않는다
    _insert_order(conn, created_at="2026-10-17T23:30:00+00:00", amount="1000")
    _insert_order(conn, created_at="2026-10-17T02:00:00+00:00", amount="500")
    rows = performance._orders_between(
        conn, "paper-main", "KR", date(2026, 10, 17), date(2026, 10, 17)
    )
    assert [r["amount"] for r in rows] == ["500"]


def test_report_quant_benchmark_passes_universe_to_latest_rows(conn, project, monkeypatch):
    """퀀트 벤치마크의 최신 행 조회는 유니버스 종목을 넘겨 파일 60개를 전부 읽지 않는다."""
    import yaml

    from trader.ml import features, ranker

    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["A", "B"], "us": [], "watch": [], "exclude": []}), encoding="utf-8"
    )
    seen = {}

    def fake_latest_rows(state, market, *, tickers=None, asof=None, max_files=60):
        seen["tickers"] = tickers
        seen["asof"] = asof
        return pd.DataFrame()

    monkeypatch.setattr(features, "latest_rows", fake_latest_rows)
    monkeypatch.setattr(ranker, "current", lambda state=None, **kw: (lambda df: df, "v1"))
    _navs(conn, [100, 110])
    r = performance.report(conn, None, "paper-main", "KR", days=30)
    assert seen["tickers"] == ["A", "B"]
    # 퀀트 벤치마크 종목은 **창 시작** 시점 피처로 고른다 (창 끝 피처는 룩어헤드)
    assert seen["asof"].isoformat() == r["nav"][0]["time"]
