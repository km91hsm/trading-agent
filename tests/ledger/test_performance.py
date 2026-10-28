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


def _shadow_quant(conn, day, weights):
    import json

    conn.execute(
        "INSERT INTO shadow_portfolios(market, date, strategy, weights_json, created_at) "
        "VALUES ('KR', ?, 'quant_topn', ?, 'x')",
        (day.isoformat(), json.dumps(weights)),
    )
    conn.commit()


def test_report_quant_benchmark_uses_recorded_shadow_not_current_model(conn, project, monkeypatch):
    """L14: 성과 페이지의 퀀트 기준선은 현재 모델을 과거 창에 소급 적용하지 않고, 그날 기록된 섀도
    `quant_topn`(당시 정량 스크린)을 다음 봉부터 따른다. 기록 전은 N/A, 바구니 교체는 왕복비용."""
    from trader.ml import ranker

    monkeypatch.setattr(
        ranker, "current", lambda *a, **k: (_ for _ in ()).throw(AssertionError("소급 금지"))
    )
    _store({"A": [100, 100, 110, 110, 121] + [121] * 20, "B": [100] * 25})
    _navs(conn, [100, 100, 100, 100, 100])
    r = performance.report(conn, None, "paper-main", "KR", days=30)
    assert r["benchmarks"]["quant"] is None  # 섀도 기록 없음
    _shadow_quant(conn, DAYS[1], {"A": 1.0})
    q = performance.report(conn, None, "paper-main", "KR", days=30)["benchmarks"]["quant"]
    assert q["source"] == "shadow_quant_topn" and q["since"] == DAYS[1].isoformat()
    # DAYS[1] 종가에 들어가 → DAYS[2] +10%, DAYS[4] +10%. 진입 편도 비용 15bp(왕복 30bp 의 절반)
    assert q["return"] == pytest.approx(0.9985 * 1.10 * 1.10 - 1, abs=1e-6)
    assert q["series"][0]["time"] == DAYS[1].isoformat()


def test_shadow_quant_benchmark_holds_drifted_weights_between_records(conn, project):
    """PR #63 Codex: 기록 사이에는 바구니를 그대로 들고(비중이 가격 따라 표류) 새 기록이 올 때만
    표류한 비중에서 목표로 리밸런스한다 — 매일 목표 비중으로 되돌리면 숨은 리밸런스 수익·비용이
    생긴다.
    50/50 에 A 100→200→100, B 그대로면 들고 있기만 한 바구니는 0%(진입 편도 비용만)."""
    _store({"A": [100, 200, 100] + [100] * 22, "B": [100] * 25})
    _navs(conn, [100, 100, 100])
    _shadow_quant(conn, DAYS[0], {"A": 0.5, "B": 0.5})
    q = performance.shadow_quant_benchmark(conn, None, "KR", DAYS[0], DAYS[2], cost_bp=30)
    assert q["return"] == pytest.approx(0.9985 - 1, abs=1e-9)


def test_metrics_ignore_zero_nav_days():
    """NAV 0 은 '그 통화로 가진 돈이 없음'(예: 원화만 있는 모의 계좌의 US) — 전액 손실(−100%)로
    세지 않는다. 0 인 날을 빼고 남은 양수 NAV 로만 계산한다."""
    from decimal import Decimal

    m = performance.metrics_of([("2026-10-20", Decimal("10000")), ("2026-10-28", Decimal("0"))])
    assert m["return"] is None and m["mdd"] is None
    m = performance.metrics_of(
        [("2026-10-20", Decimal("0")), ("2026-10-21", Decimal("10000")),
         ("2026-10-22", Decimal("0")), ("2026-10-23", Decimal("10100"))]
    )  # fmt: skip
    assert m["return"] == 0.01 and m["mdd"] == 0.0 and m["start"] == "2026-10-21"
