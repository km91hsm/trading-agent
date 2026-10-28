"""정량 스크린 과거 검증(10-26 검토 L15): 순수 함수 `scan.backtest.run`."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from trader.scan import backtest


def _frame(n_days=14, growth=None, missing=None):
    """6종목 × n_days. 종목 i 는 하루 growth[i] 씩 오른다(roc_10 도 그만큼 큼)."""
    growth = growth or [0.000, 0.002, 0.004, 0.006, 0.008, 0.010]
    days = [date(2026, 1, 5) + timedelta(days=k) for k in range(n_days)]
    rows = []
    for i, g in enumerate(growth):
        for k, d in enumerate(days):
            close = 100 * (1 + g) ** k
            rows.append(
                {
                    "date": pd.Timestamp(d),
                    "ticker": f"T{i}",
                    "market": "KR",
                    "close": close,
                    "volume": 1_000_000 + i * 100_000,  # 첫날 거래대금이 같으면 필터가 전부 거른다
                    "roc_10": g * 1000,
                    "high_52w_pos": 0.5,
                    "atr_14": close * 0.01,
                }
            )
    df = pd.DataFrame(rows)
    if missing:
        t, d = missing
        df.loc[(df["ticker"] == t) & (df["date"] == pd.Timestamp(d)), "close"] = np.nan
    return df, days


def test_windows_do_not_overlap_and_enter_next_day():
    df, days = _frame()
    r = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=0)
    w = r["windows"]
    assert w and w[0]["signal_date"] == days[0].isoformat()
    assert w[0]["entry"] == days[1].isoformat() and w[0]["exit"] == days[6].isoformat()
    assert w[1]["signal_date"] == days[5].isoformat()  # 다음 창은 horizon 만큼 뒤(겹치지 않음)
    assert r["overlapping"] is False


def test_momentum_and_screen_beat_equal_weight_when_momentum_persists():
    df, _ = _frame()
    r = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=0)
    s = r["summary"]
    assert set(s) == {"momentum", "screen", "universe_1n"}
    assert s["momentum"]["mean"] > s["universe_1n"]["mean"]
    assert s["momentum"]["excess_mean"] > 0 and s["momentum"]["windows"] == len(r["windows"])


def test_costs_are_charged_on_every_strategy():
    df, _ = _frame()
    free = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=0)["summary"]
    paid = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=30)["summary"]
    for k in ("momentum", "screen", "universe_1n"):
        assert abs((free[k]["mean"] - paid[k]["mean"]) - 0.003) < 1e-9, k


def test_missing_exit_price_is_counted_not_filled():
    days = [date(2026, 1, 5) + timedelta(days=k) for k in range(14)]
    df, _ = _frame(missing=("T5", days[6]))  # 첫 창 청산일에 최상위 종목 종가가 없다
    r = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=0)
    assert r["windows"][0]["missing"] >= 1
    assert r["missing_total"] >= 1


def test_empty_or_short_input():
    df, _ = _frame(n_days=4)
    r = backtest.run(df, market="KR", top_n=2, horizon=5, cost_bp=0)
    assert r["windows"] == [] and r["summary"]["momentum"]["windows"] == 0


def test_cli_screen_backtest_reads_feature_store(project):
    """`trader screen backtest` 는 피처 스토어만 읽어 JSON 한 줄(설정·주문 불변)."""
    import json

    from typer.testing import CliRunner

    from trader.cli import app
    from trader.ml import features

    df, _ = _frame()
    full = df.copy()
    for c in features.FEATURE_COLUMNS:
        if c not in full.columns:
            full[c] = np.nan
    features.write_rows(full[features.FEATURE_COLUMNS])
    r = CliRunner().invoke(
        app, ["screen", "backtest", "--market", "KR", "--days", "3650", "--top", "2"]
    )
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["cost_bp"] == 30 and out["summary"]["momentum"]["windows"] >= 1
    assert "pit" in out and "survivorship" in out["note"]
