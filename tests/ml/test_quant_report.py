from datetime import UTC, datetime

import pytest
from helpers_ml import synthetic_features
from pydantic import ValidationError

from trader.agents import schemas
from trader.ml import dataset, features, quant_report, registry
from trader.ml.models import quant

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _promoted(project):
    df = dataset.make_labels(
        synthetic_features(3, 120, signal=True), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    m = quant.QuantModel(cols).fit(X, y)
    v = registry.save("quant", m, {"features": cols, "label": {"horizon_days": 5}}, now=lambda: NOW)
    registry.promote("quant", v)
    features.write_rows(synthetic_features(3, 120, signal=True))
    return m, cols, v


def test_quant_report_schema_and_signal():
    base = dict(
        ticker="005930",
        asof="2026-10-17T00:00:00+00:00",
        role="quant",
        model="quant@v",
        p_up=0.6,
        p_flat=0.3,
        p_down=0.1,
        horizon_days=5,
        features_used=["roc_10"],
        data_gaps=[],
    )
    r = schemas.QuantReport(**base, signal="BUY")
    assert r.signal == "BUY"
    assert (
        quant_report.signal_for(0.6, 0.1) == "BUY" and quant_report.signal_for(0.2, 0.55) == "SELL"
    )
    assert quant_report.signal_for(0.4, 0.3) == "NEUTRAL"
    with pytest.raises(ValidationError):
        schemas.QuantReport(**{**base, "p_up": 1.5}, signal="BUY")
    assert schemas.schema_for("05_quant") is schemas.QuantReport


def test_build_report_from_latest_row(project):
    m, cols, v = _promoted(project)
    row = quant_report.latest_row("T00", "KR")
    assert row is not None and row["ticker"] == "T00"
    model, meta = registry.latest("quant")
    rep = quant_report.build(model, meta, row, ticker="T00", asof=NOW.isoformat())
    assert rep["role"] == "quant" and rep["model"] == f"quant@{v}" and rep["horizon_days"] == 5
    assert abs(rep["p_up"] + rep["p_flat"] + rep["p_down"] - 1) < 1e-6 and rep["signal"] in (
        "BUY",
        "SELL",
        "NEUTRAL",
    )
    assert rep["features_used"] == cols and rep["feature_date"] == row["date"]
    assert quant_report.latest_row("ZZZZ", "KR") is None


def test_predict_aligns_feature_columns(project):
    m, cols, v = _promoted(project)
    model, meta = registry.latest("quant")
    row = {c: 1.0 for c in cols[1:]}  # 첫 피처 누락 + 여분 열
    row["extra"] = 9.9
    rep = quant_report.build(model, meta, row, ticker="X", asof=NOW.isoformat())
    assert cols[0] in rep["data_gaps"][0]


def test_latest_row_respects_asof(project):
    from datetime import date

    features.write_rows(synthetic_features(1, 30, signal=False))
    newest = quant_report.latest_row("T00", "KR")
    capped = quant_report.latest_row("T00", "KR", asof=date(2025, 1, 10))
    assert newest["date"] > capped["date"] and capped["date"] <= "2025-01-10"
    assert quant_report.latest_row("T00", "KR", asof=date(2024, 1, 1)) is None


def test_latest_row_is_memoized_per_process(project, monkeypatch):
    """같은 tool 서브프로세스 안에서 같은 종목을 3번 조회한다(메모리·보정·05_quant). 피처 스토어에
    없는 종목은 조회마다 60파일(0.9초)을 읽으므로 (종목·시장·asof·최신 파일) 키로 메모이즈한다."""
    from helpers_ml import synthetic_features

    from trader.ml import features

    features.write_rows(synthetic_features(1, 30, signal=False))
    calls = {"n": 0}
    real = features.latest_rows

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(quant_report.features, "latest_rows", counting)
    quant_report.latest_row.cache_clear()
    r1 = quant_report.latest_row("T00", "KR")
    r2 = quant_report.latest_row("T00", "KR")
    assert r1 == r2 and r1 is not None and calls["n"] == 1
    assert quant_report.latest_row("NOPE", "KR") is None
    assert quant_report.latest_row("NOPE", "KR") is None and calls["n"] == 2
    # 새 파일이 생기면 키가 바뀌어 다시 읽는다
    features.write_rows(synthetic_features(1, 31, signal=False))
    quant_report.latest_row("T00", "KR")
    assert calls["n"] == 3
