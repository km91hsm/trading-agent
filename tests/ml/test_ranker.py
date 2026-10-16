from datetime import UTC, datetime

from helpers_ml import synthetic_features

from trader.ml import dataset, features, ranker, registry
from trader.ml.models import quant
from trader.scan import quant as scan_quant

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def test_rank_fn_falls_back_without_model(project):
    assert ranker.rank_fn_or_none() is None


def test_rank_fn_reorders_by_p_up(project):
    df = dataset.make_labels(
        synthetic_features(4, 120, signal=True), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    v = registry.save(
        "quant", quant.QuantModel(cols).fit(X, y), {"features": cols}, now=lambda: NOW
    )
    registry.promote("quant", v)
    fn = ranker.rank_fn_or_none()
    assert fn is not None
    latest = df.sort_values("date").groupby("ticker", as_index=False).tail(1).reset_index(drop=True)
    out = fn(latest)
    assert set(out.columns) >= {"ticker", "market", "score", "reasons", "p_up"}
    assert out["score"].is_monotonic_decreasing
    assert any("ML P(up)" in r for r in out["reasons"].iloc[0])
    plain = scan_quant.score(latest)
    assert set(plain["ticker"]) == set(out["ticker"]) and len(out) == len(plain)  # 필터 동일
    features.write_rows(synthetic_features(4, 5, signal=False))
