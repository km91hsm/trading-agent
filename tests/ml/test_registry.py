from datetime import UTC, datetime

from helpers_ml import synthetic_features

from trader.ml import dataset, registry
from trader.ml.models import quant

NOW = datetime(2026, 10, 17, 1, 2, 3, tzinfo=UTC)


def _model():
    df = dataset.make_labels(
        synthetic_features(2, 80, signal=False), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    return quant.QuantModel(cols).fit(X, y), cols


def test_registry_save_list_latest_promote(project):
    m, cols = _model()
    assert registry.latest("quant") is None and registry.list_models("quant") == []
    v1 = registry.save(
        "quant", m, {"features": cols, "metrics": {"accuracy": 0.5}}, now=lambda: NOW
    )
    assert v1 == "20261017_010203"
    assert (project / "state" / "models" / "quant" / v1 / "model.joblib").exists()
    assert registry.latest("quant") is None  # 미승격
    model, meta = registry.latest("quant", promoted_only=False)
    assert meta["version"] == v1 and meta["promoted"] is False and meta["features"] == cols
    assert model.predict_proba(
        quant.align(synthetic_features(1, 20, signal=False), cols)[:1]
    ).shape == (1, 3)
    r = registry.promote("quant", v1, reason="테스트")
    assert r["promoted"] is True and r["promote_reason"] == "테스트"
    _, meta = registry.latest("quant")
    assert meta["version"] == v1 and meta["promoted"] is True
    v2 = registry.save(
        "quant", m, {"features": cols}, now=lambda: datetime(2026, 10, 18, tzinfo=UTC)
    )
    assert [x["version"] for x in registry.list_models("quant")] == [v2, v1]
    assert registry.latest("quant")[1]["version"] == v1  # 승격된 최신은 v1


def test_latest_skips_broken_model_file(project):
    m, cols = _model()
    v = registry.save("quant", m, {"features": cols}, now=lambda: NOW)
    registry.promote("quant", v)
    (project / "state" / "models" / "quant" / v / "model.joblib").write_bytes(b"garbage")
    assert registry.latest("quant") is None  # 깨진 파일은 건너뛴다 (예외 전파 없음)


def test_demote(project):
    m, cols = _model()
    v = registry.save("quant", m, {"features": cols}, now=lambda: NOW)
    registry.promote("quant", v)
    r = registry.demote("quant", v, reason="수동 해제")
    assert r["promoted"] is False and registry.latest("quant") is None


def test_latest_warns_with_backend_hint_when_lightgbm_model_fails_to_load(project, caplog):
    """lightgbm 으로 학습한 승격 모델을 lightgbm 없는 환경(번들)이 못 읽으면 원인을 알려야 한다
    (리뷰 #8)."""
    import logging

    d = project / "state" / "models" / "quant" / "20260101_000000"
    d.mkdir(parents=True)
    (d / "model.joblib").write_bytes(b"not a pickle")
    (d / "meta.json").write_text(
        '{"name": "quant", "version": "20260101_000000", "promoted": true, "backend": "lightgbm"}',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="trader.ml"):
        assert registry.latest("quant") is None
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "lightgbm" in msg and ("sklearn" in msg or "demote" in msg)


def test_latest_prefers_market_model_and_never_uses_other_market(project):
    """`train --market KR` 모델은 US scan 에 쓰이지 않는다. 시장 모델이 없으면 전시장 모델로
    폴백."""
    m, cols = _model()
    v_all = registry.save("quant", m, {"features": cols}, now=lambda: NOW)
    registry.promote("quant", v_all)
    v_kr = registry.save(
        "quant",
        m,
        {"features": cols, "market": "KR"},
        now=lambda: datetime(2026, 10, 18, tzinfo=UTC),
    )
    registry.promote("quant", v_kr)
    assert registry.latest("quant", market="KR")[1]["version"] == v_kr
    assert registry.latest("quant", market="US")[1]["version"] == v_all  # 전시장 폴백
    assert registry.latest("quant")[1]["version"] == v_kr  # 시장 미지정은 최신
    registry.demote("quant", v_all)
    assert registry.latest("quant", market="US") is None


def test_load_is_memoized_until_meta_changes(project, monkeypatch):
    import joblib

    m, cols = _model()
    v = registry.save("quant", m, {"features": cols}, now=lambda: NOW)
    calls = []
    orig = joblib.load
    monkeypatch.setattr(joblib, "load", lambda p: calls.append(p) or orig(p))
    registry.load("quant", v)
    registry.load("quant", v)
    assert len(calls) == 1
    registry.promote("quant", v)  # 메타가 바뀌면 다시 읽는다
    import os
    import time

    meta = project / "state" / "models" / "quant" / v / "meta.json"
    os.utime(meta, (time.time() + 5, time.time() + 5))
    registry.load("quant", v)
    assert len(calls) == 2
