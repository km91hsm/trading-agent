"""섀도 포트폴리오(전체 검토 L1): LLM 선택 vs 같은 후보 1/N·정량 상위 — 쌍 비교로 알파를 잰다."""

from datetime import date

import numpy as np
import pandas as pd
from helpers_ml import trading_days

from trader.ledger import shadow
from trader.ml import features as feat

D0 = date(2026, 9, 1)
DAYS = trading_days(30, start=D0)


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


def _decision(conn, ticker, action, w, *, source="llm", d=None):
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, json, "
        "created_at, source) VALUES (?, 'KR', ?, ?, ?, 0.7, '{}', '2026-09-01T00:00:00+00:00', ?)",
        ((d or DAYS[0]).isoformat(), ticker, action, w, source),
    )
    conn.commit()


SCAN = {
    "candidates": [
        {"ticker": "A", "score": 0.9},
        {"ticker": "B", "score": 0.5},
        {"ticker": "C", "score": 0.1},
        {"ticker": "D", "score": None},  # 피처 없음 → 1/N 에서도 제외
    ]
}


def test_record_builds_three_portfolios(conn):
    _decision(conn, "B", "BUY", 0.2)
    _decision(conn, "C", "HOLD", 0.0)
    _decision(conn, "X", "BUY", 0.1, source="hedge")  # 헤지는 LLM 선택이 아니다
    n = shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    assert n == 3
    got = {r["strategy"]: r["weights"] for r in shadow.rows(conn, market="KR")}
    assert got["candidates_1n"] == {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
    assert got["quant_topn"] == {"A": 0.5, "B": 0.5}
    assert got["llm"] == {"B": 1.0}
    # 같은 날 재실행(실패 뒤 재시도)은 만기 전 행을 덮어쓴다 — 행 수는 그대로
    _decision(conn, "A", "ADD", 0.1)
    shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    got = {r["strategy"]: r["weights"] for r in shadow.rows(conn, market="KR")}
    assert len(shadow.rows(conn, market="KR")) == 3
    assert got["llm"] == {"A": 1 / 3, "B": 2 / 3}


def test_llm_without_buys_is_cash(conn):
    _decision(conn, "A", "HOLD", 0.0)
    shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    got = {r["strategy"]: r["weights"] for r in shadow.rows(conn, market="KR")}
    assert got["llm"] == {}


def test_mature_fills_forward_return_only_after_horizon(conn, project):
    _store({"A": [100.0] * 7, "B": [100, 101, 102, 103, 104, 110, 111], "C": [100.0] * 7})
    _decision(conn, "B", "BUY", 0.2)
    shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    # 만기 봉(DAYS[5]) 전이면 채우지 않는다
    assert shadow.mature(conn, state=project / "state", today=DAYS[4], horizon=5) == 0
    assert shadow.mature(conn, state=project / "state", today=DAYS[5], horizon=5) == 3
    got = {r["strategy"]: r["fwd_ret"] for r in shadow.rows(conn, market="KR")}
    assert abs(got["llm"] - 0.10) < 1e-9
    assert abs(got["quant_topn"] - 0.05) < 1e-9
    assert abs(got["candidates_1n"] - 0.10 / 3) < 1e-9
    # 멱등, 만기된 행은 재기록이 덮지 않는다
    assert shadow.mature(conn, state=project / "state", today=DAYS[6], horizon=5) == 0
    _decision(conn, "A", "BUY", 0.3)
    shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    after = {r["strategy"]: (r["weights"], r["fwd_ret"]) for r in shadow.rows(conn, market="KR")}
    assert after["llm"] == ({"B": 1.0}, got["llm"])


def test_mature_cash_portfolio_is_zero_when_window_closed(conn, project):
    _store({"A": [100.0] * 7, "B": [100.0] * 7, "C": [100.0] * 7})
    shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2)
    shadow.mature(conn, state=project / "state", today=DAYS[6], horizon=5)
    got = {r["strategy"]: r["fwd_ret"] for r in shadow.rows(conn, market="KR")}
    assert got["llm"] == 0.0


def _seed_matured(conn, diffs: list[float]):
    for i, dlt in enumerate(diffs):
        d = date(2026, 1, 1).toordinal() + i
        day = date.fromordinal(d).isoformat()
        for strat, ret in (("llm", 0.01 + dlt), ("candidates_1n", 0.01)):
            conn.execute(
                "INSERT INTO shadow_portfolios(market, date, strategy, weights_json, created_at, "
                "fwd_ret, matured_at) VALUES ('KR', ?, ?, '{}', ?, ?, ?)",
                (day, strat, day, ret, day),
            )
    conn.commit()


def test_paired_stats_ci_positive_when_llm_consistently_better(conn):
    _seed_matured(conn, [0.004 + 0.001 * (i % 3) for i in range(25)])
    s = shadow.paired_stats(conn)
    assert s["n"] == 25
    assert s["ci_low"] > 0 and s["mean_diff"] > 0


def test_paired_stats_ci_straddles_zero_for_noise(conn):
    _seed_matured(conn, [0.02 if i % 2 else -0.02 for i in range(24)])
    s = shadow.paired_stats(conn)
    assert s["ci_low"] < 0 < s["ci_high"]


def test_paired_stats_empty(conn):
    s = shadow.paired_stats(conn)
    assert s == {"n": 0, "mean_diff": None, "ci_low": None, "ci_high": None}


def test_block_bootstrap_is_wider_for_clustered_differences():
    """연속 운영일의 5거래일 수익은 겹친다 → 블록 부트스트랩 구간이 독립 가정보다 넓다."""
    d = np.array(([0.02] * 5 + [-0.01] * 5) * 3)
    iid = shadow._block_bootstrap_means(d, block=1, n_boot=4000, seed=1)
    blk = shadow._block_bootstrap_means(d, block=5, n_boot=4000, seed=1)
    assert np.std(blk) > np.std(iid)
    assert shadow._block_bootstrap_means(d[:2], block=5, n_boot=10, seed=0).shape == (10,)


def _index(closes: list[float], days=None):
    days = days or DAYS
    return pd.DataFrame({"069500": closes}, index=pd.Index(days[: len(closes)], name="date"))


def test_index_baselines_hold_and_trend():
    """지수 보유·지수 추세(100일선 위면 보유, 아래면 현금) — Stock D-037~039: 규칙이 지수 보유를
    못 이겼다. LLM 이 지수보다 나은지가 실전 전환의 핵심 질문."""
    long_days = trading_days(130, start=date(2026, 3, 2))
    up = _index([100 + i for i in range(130)], long_days)
    down = _index([300 - i for i in range(130)], long_days)
    day = long_days[-1]
    w_up = shadow.index_weights(up, day=day)
    assert w_up == {"index_hold": {"069500": 1.0}, "index_trend": {"069500": 1.0}}
    w_down = shadow.index_weights(down, day=day)
    assert w_down == {"index_hold": {"069500": 1.0}, "index_trend": {}}
    assert shadow.index_weights(_index([100.0] * 50, long_days), day=day)["index_trend"] == {}
    assert shadow.index_weights(pd.DataFrame(), day=day) == {}


def test_record_and_mature_with_index_table(conn, project):
    _store({"A": [100.0] * 7, "B": [100.0] * 7, "C": [100.0] * 7})
    idx = _index([100, 100, 101, 102, 103, 104, 110.0])
    n = shadow.record(conn, market="KR", day=DAYS[0], scan=SCAN, top_n=2, index=idx)
    assert n == 5
    got = {r["strategy"]: r["weights"] for r in shadow.rows(conn, market="KR")}
    assert got["index_hold"] == {"069500": 1.0}
    # 지수 proxy 는 피처 스토어에 없다 → 만기 때 넘겨 준 지수 종가표로 채운다
    assert (
        shadow.mature(
            conn, state=project / "state", today=DAYS[6], horizon=5, index_tables={"KR": idx}
        )
        == 5
    )
    rets = {r["strategy"]: r["fwd_ret"] for r in shadow.rows(conn, market="KR")}
    assert abs(rets["index_hold"] - 0.04) < 1e-9  # DAYS[0]=100 → 5거래일 뒤 104
    assert rets["index_trend"] == 0.0  # 100일 미만 이력 → 현금
    s = shadow.paired_stats(conn, b="index_hold")
    assert s["n"] == 1
