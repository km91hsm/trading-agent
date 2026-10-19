from helpers_memory import seed_matured
from helpers_ml import synthetic_features

from trader.ml import features
from trader.ml.rl import dataset as rl_dataset


def test_steps_from_features_when_memory_sparse(project, conn):
    features.write_rows(synthetic_features(3, 60, signal=False))
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert set(df["source"]) == {"features"} and len(df["obs"].iloc[0]) == 14
    assert df["obs"].iloc[0][-2:] == [0.5, 0.0]


def test_steps_drop_rows_without_forward_return(project, conn):
    features.write_rows(synthetic_features(1, 20, signal=False))
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert len(df) == 15 and df["fwd_return"].notna().all()


def test_steps_prefer_memory_when_enough(project, conn):
    seed_matured(conn, 12, hit_rate=0.5)
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=10)
    assert set(df["source"]) == {"memory"} and len(df) == 12
    assert df["obs"].iloc[0][-2] == 0.6  # confidence
    assert df["fwd_return"].abs().round(2).eq(0.03).all()


def test_steps_empty_when_no_data(project, conn):
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert df.empty and list(df.columns) == ["date", "ticker", "obs", "fwd_return", "source"]


def test_memory_steps_carry_horizon_days(project, conn):
    seed_matured(conn, 12, hit_rate=0.5)
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=10)
    assert "horizon_days" in df.columns and set(df["horizon_days"]) == {5}


def test_memory_steps_only_buy_add(project, conn):
    from datetime import date

    seed_matured(conn, 6, action="HOLD")
    seed_matured(conn, 6, action="BUY", start=date(2026, 3, 1))
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=1)
    assert len(df) == 6 and set(df["source"]) == {"memory"}


def test_memory_step_date_is_run_date_in_kst(project, conn):
    """07:50 KST 결정(decided_at 22:50 UTC 전날)의 스텝 날짜는 운영일(KST)이어야 한다.
    UTC 날짜로 묶으면 전날 스텝과 섞여 폴드 경계·엠바고가 하루 어긋난다."""
    from datetime import UTC, datetime

    from trader.memory import store

    eid = store.record_decision(
        conn,
        decision_id=1,
        ticker="T000",
        market="KR",
        account_id="paper-main",
        decided_at=datetime(2026, 10, 9, 22, 50, tzinfo=UTC).isoformat(),  # 10-10 07:50 KST
        decision={"action": "BUY", "target_weight": 0.1, "confidence": 0.6},
        row=None,
        horizon_days=5,
        vector=[0.5] * 12,
    )
    store.fill_outcome(
        conn, eid, realized_return=0.02, alpha=0.0, outcome_at="x", price_return=0.02
    )
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=1)
    assert df["date"].iloc[0].date().isoformat() == "2026-10-10"
