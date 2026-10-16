import json
from datetime import date

import pandas as pd
import yaml
from typer.testing import CliRunner

from trader import cli
from trader.cli import app
from trader.ml import features


def _setup(project, monkeypatch):
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["000001", "000002", "000003"], "watch": [], "exclude": []}),
        encoding="utf-8",
    )
    rows = [
        {
            "date": date(2026, 10, 8),
            "ticker": t,
            "market": "KR",
            "close": 100.0,
            "volume": v,
            "atr_14": 1.0,
            "roc_10": r,
            "high_52w_pos": 0.5,
            "vol_z_20": 0.0,
        }
        for t, v, r in (
            ("000001", 1_000_000, 5.0),
            ("000002", 2_000_000, 1.0),
            ("000003", 3_000_000, 3.0),
        )
    ]
    features.write_rows(_full(pd.DataFrame(rows)))
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    monkeypatch.setattr(cli, "_build_brokers", lambda s, a: [])


def test_cli_scan_fake(project, monkeypatch):
    _setup(project, monkeypatch)
    r = CliRunner().invoke(app, ["scan", "--market", "KR", "--asof", "2026-10-09T00:00:00+00:00"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["market"] == "KR" and len(out["candidates"]) == 3
    # 거래대금 하위 30%(000001) 제외, 모멘텀 순
    assert [p["ticker"] for p in out["picks"]] == ["000003", "000002"]
    f = project / "state" / "runs" / "2026-10-09" / "KR" / "_scan.json"
    assert json.loads(f.read_text(encoding="utf-8"))["analyze_targets"][0]["ticker"] == "000003"


def test_cli_scan_rejects_bad_market(project, monkeypatch):
    _setup(project, monkeypatch)
    assert CliRunner().invoke(app, ["scan", "--market", "XX"]).exit_code == 1


def _full(df):
    """피처 스토어가 요구하는 전체 열을 채운다 (없는 열은 NaN)."""
    import numpy as np

    from trader.ml import features as _f

    for c in _f.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df


def test_cli_scan_uses_ml_rank_when_promoted(project, monkeypatch):
    from datetime import UTC, datetime

    from helpers_ml import synthetic_features

    from trader.ml import dataset, registry
    from trader.ml.models import quant

    _setup(project, monkeypatch)
    df = dataset.make_labels(
        synthetic_features(3, 120, signal=True), horizon_days=5, up=0.02, down=-0.02
    )
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    v = registry.save(
        "quant",
        quant.QuantModel(cols).fit(X, y),
        {"features": cols},
        now=lambda: datetime(2026, 10, 17, tzinfo=UTC),
    )
    registry.promote("quant", v)
    r = CliRunner().invoke(
        app, ["scan", "--market", "KR", "--asof", "2026-10-09T00:00:00+00:00", "--dry-run"]
    )
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ml_model"] == f"quant@{v}"
    assert any("ML P(up)" in rs for c in out["candidates"] for rs in c["reasons"])
