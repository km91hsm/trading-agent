from datetime import UTC, datetime

import pytest
from helpers_memory import seed_matured

from trader.calibration import model
from trader.config import MlConfig

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def test_signal_hits_join_and_sign_mapping(conn):
    seed_matured(conn, 10, hit_rate=0.7)
    df = model.signal_hits(conn)
    assert len(df) == 40 and set(df["role"]) == {
        "technical",
        "fundamental",
        "news_macro",
        "sentiment",
    }
    assert set(df["signal"]) == {1, -1} and set(df["realized_sign"]) == {1, -1}
    assert df[df["role"] == "sentiment"]["signal"].eq(-1).all()
    assert len(df["vector"].iloc[0]) == 12


def test_hit_rates_overall_and_neighbors(conn):
    seed_matured(conn, 20, hit_rate=0.7)
    df = model.signal_hits(conn)
    r = model.hit_rates(df, query_vector=[0.5] * 12, k=20)
    assert abs(r["technical"]["hit_rate"] - 0.7) < 1e-9 and r["technical"]["n"] == 20
    assert abs(r["sentiment"]["hit_rate"] - 0.3) < 1e-9  # SELL 신호는 상승 때 빗나감
    assert (
        r["technical"]["neighbor_n"] == 20 and abs(r["technical"]["neighbor_hit_rate"] - 0.7) < 1e-9
    )
    assert r["sentiment"]["bias"] < 0 < r["technical"]["bias"]
    r2 = model.hit_rates(df, query_vector=None)
    assert r2["technical"]["neighbor_hit_rate"] is None


def test_calibration_inactive_below_60(conn):
    seed_matured(conn, 59)
    assert (
        model.build_calibration(conn, min_decisions=60, query_vector=None, asof=NOW.isoformat())
        is None
    )
    assert conn.execute("SELECT active FROM calibration_runs").fetchone()["active"] == 0


def test_calibration_active_at_60(conn):
    seed_matured(conn, 60, hit_rate=0.6)
    r = model.build_calibration(
        conn, min_decisions=60, query_vector=[0.5] * 12, asof=NOW.isoformat()
    )
    assert (
        r["active"] is True
        and r["n_decisions"] == 60
        and set(r["roles"]) >= {"technical", "sentiment"}
    )
    assert abs(r["roles"]["technical"]["hit_rate"] - 0.6) < 1e-9
    row = conn.execute(
        "SELECT active, n_decisions FROM calibration_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["active"] == 1 and row["n_decisions"] == 60
    assert MlConfig().models.calibration.min_decisions == 60


def test_signal_hits_uses_price_return_for_hold(conn):
    """HOLD 결정은 실현수익이 0 이지만 가격 방향(price_return)으로 신호를 채점한다."""
    seed_matured(conn, 10, action="HOLD", hit_rate=1.0)
    df = model.signal_hits(conn)
    assert len(df) == 40 and (df["realized_sign"] == 1).all()
    rates = model.hit_rates(df, query_vector=None)
    assert rates["technical"]["hit_rate"] == pytest.approx(1.0)
    assert rates["sentiment"]["hit_rate"] == pytest.approx(0.0)  # SELL 신호는 빗나감


def test_build_calibration_one_row_per_day(conn):
    seed_matured(conn, 3)
    for _ in range(2):
        model.build_calibration(
            conn, min_decisions=1, query_vector=None, asof="2026-10-17T00:00:00+00:00"
        )
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 1


def test_build_calibration_day_key_is_kst(conn):
    seed_matured(conn, 3)
    # 10-17 23:30Z = 10-18 08:30 KST, 10-18 02:00Z = 10-18 11:00 KST → 같은 KST 날: 1행
    for now in (datetime(2026, 10, 17, 23, 30, tzinfo=UTC), datetime(2026, 10, 18, 2, tzinfo=UTC)):
        model.build_calibration(
            conn, min_decisions=1, query_vector=None, asof=now.isoformat(), now=lambda n=now: n
        )
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 1
    model.build_calibration(
        conn,
        min_decisions=1,
        query_vector=None,
        asof="x",
        now=lambda: datetime(2026, 10, 17, 10, tzinfo=UTC),  # 10-17 19:00 KST → 다른 날
    )
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 2
