from datetime import UTC, datetime

from helpers_ml import synthetic_features

from trader.ml import dataset, registry
from trader.ml.models import quant

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _save(promoted: bool):
    df = dataset.make_labels(
        synthetic_features(2, 80, signal=False), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    v = registry.save(
        "quant",
        quant.QuantModel(cols).fit(X, y),
        {
            "features": cols,
            "metrics": {
                "accuracy": 0.41,
                "alpha": 0.001,
                "folds": [{"index": 0, "accuracy": 0.41}],
            },
            "importances": {c: 0.01 for c in cols},
        },
        now=lambda: NOW,
    )
    if promoted:
        registry.promote("quant", v, reason="기준 통과")
    return v


def test_api_ml_models_lists_registry(client):
    assert client.get("/api/ml/models").json()["models"] == []
    v = _save(promoted=True)
    r = client.get("/api/ml/models").json()
    assert r["models"][0]["version"] == v and r["models"][0]["promoted"] is True
    assert r["active"]["version"] == v and "importances" not in r["models"][0]  # 목록은 요약
    d = client.get(f"/api/ml/models/{v}").json()
    assert d["importances"] and d["metrics"]["folds"][0]["accuracy"] == 0.41
    assert client.get("/api/ml/models/nope").status_code in (400, 404)
    assert client.get("/api/ml/models/20990101_000000").status_code == 404
    assert client.get("/api/ml/models/../x").status_code in (400, 404)
