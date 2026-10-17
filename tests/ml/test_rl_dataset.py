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
