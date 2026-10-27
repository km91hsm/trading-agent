"""포트폴리오 위험 카드(Stock D-088): 보유 비중 × 최근 60거래일 수익 → VaR·CVaR 95%, 유니버스
1/N 대비 베타, 상관, 위험 기여(합 1). 표시 전용."""

from datetime import date

import numpy as np
import pandas as pd
from helpers_ml import trading_days

from trader.ledger import risk_card
from trader.ml import features as feat

DAYS = trading_days(80, start=date(2026, 6, 1))


def _store(series: dict[str, list[float]]):
    rows = []
    for t, closes in series.items():
        for d, c in zip(DAYS, closes, strict=True):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": c, "volume": 1})
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    feat.write_rows(df[feat.FEATURE_COLUMNS])


def _snap(conn, d, rows, nav):
    for t, ev in rows:
        conn.execute(
            "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
            "current_price, eval_amount, nav, cash, created_at) VALUES ('paper-main','KR',?,?,1,"
            "'0','0',?,?,'0','2026-09-30T07:00:00+00:00')",
            (d, t, str(ev), str(nav)),
        )
    conn.commit()


def test_risk_card_metrics(conn, project):
    rng = np.random.default_rng(0)
    base = rng.normal(0, 0.01, 80)
    a = 100 * np.cumprod(1 + base)
    b = 100 * np.cumprod(1 + base * 0.9 + rng.normal(0, 0.002, 80))  # A 와 상관 높음
    c = 100 * np.cumprod(1 + rng.normal(0, 0.02, 80))
    _store({"A": list(a), "B": list(b), "C": list(c)})
    _snap(conn, DAYS[-1].isoformat(), [("A", 500), ("B", 300)], 1000)
    r = risk_card.compute(conn, account_id="paper-main", market="KR", today=DAYS[-1])
    assert r["n_obs"] == 60 and r["asof"] == DAYS[-1].isoformat()
    assert 0 < r["var95"] < r["cvar95"]
    assert r["corr"]["tickers"] == ["A", "B"] and r["corr"]["matrix"][0][1] > 0.8
    contrib = {x["ticker"]: x for x in r["contributions"]}
    assert abs(sum(x["contribution"] for x in r["contributions"]) - 1) < 1e-9
    assert (
        contrib["A"]["weight"] == 0.5
        and contrib["A"]["contribution"] > contrib["B"]["contribution"]
    )
    assert r["beta"] is not None and r["missing"] == []


def test_risk_card_insufficient(conn, project):
    assert risk_card.compute(conn, account_id="paper-main", market="KR", today=DAYS[-1]) == {
        "insufficient": "잔고 스냅샷 없음"
    }
    _snap(conn, DAYS[-1].isoformat(), [("ZZZ", 500)], 1000)
    r = risk_card.compute(conn, account_id="paper-main", market="KR", today=DAYS[-1])
    assert r["insufficient"].startswith("종가 부족") and r["missing"] == ["ZZZ"]


def test_risk_card_ignores_future_and_thin_history(conn, project):
    rng = np.random.default_rng(1)
    full = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 80)))
    thin = [100.0, 101.0] + [float("nan")] * 78  # 처음 이틀만
    _store({"A": full, "T": thin, "B": full})
    _snap(conn, DAYS[-1].isoformat(), [("A", 500), ("T", 300)], 1000)
    r = risk_card.compute(conn, account_id="paper-main", market="KR", today=DAYS[-1])
    assert r["missing"] == ["T"] and r["partial"] is True
    assert [x["ticker"] for x in r["contributions"]] == ["A"]
    early = risk_card.compute(conn, account_id="paper-main", market="KR", today=DAYS[40])
    assert early["asof"] <= DAYS[40].isoformat()  # 조회 기준일 이후 종가는 쓰지 않는다


def test_risk_card_cache_reuses_until_snapshot_or_store_changes(conn, project, monkeypatch):
    """전체 검토 3차(성능): 같은 입력이면 다시 계산하지 않고, 스냅샷이 새로 쓰이면 다시 계산한다."""
    rng = np.random.default_rng(2)
    _store({"A": list(100 * np.cumprod(1 + rng.normal(0, 0.01, 80)))})
    _snap(conn, DAYS[-2].isoformat(), [("A", 500)], 1000)
    calls: list[int] = []
    real = risk_card.compute

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(risk_card, "compute", spy)
    risk_card._CACHE.clear()
    kw = {"account_id": "paper-main", "market": "KR", "today": DAYS[-1]}
    first = risk_card.cached_compute(conn, **kw)
    assert risk_card.cached_compute(conn, **kw) == first and len(calls) == 1
    _snap(conn, DAYS[-1].isoformat(), [("A", 600)], 1000)
    risk_card.cached_compute(conn, **kw)
    assert len(calls) == 2
