import json

from helpers_ml import synthetic_features
from typer.testing import CliRunner

from trader.cli import app
from trader.ml import features, registry


def _fast_ml_yaml(project):
    (project / "config" / "ml.yaml").write_text(
        "models:\n  quant: {max_iter: 40}\n", encoding="utf-8"
    )


def test_cli_ml_train_models_promote(project):
    _fast_ml_yaml(project)
    features.write_rows(synthetic_features(6, 320, signal=True))
    runner = CliRunner()
    r = runner.invoke(app, ["ml", "train", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["status"] == "trained" and out["version"] and out["promoted"] is True
    r = runner.invoke(app, ["ml", "models"])
    assert r.exit_code == 0 and out["version"] in r.stdout and "승격" in r.stdout
    r = runner.invoke(app, ["ml", "promote", out["version"], "--reason", "수동"])
    assert r.exit_code == 0 and registry.latest("quant")[1]["promote_reason"] == "수동"
    assert runner.invoke(app, ["ml", "promote", "nope"]).exit_code == 1


def test_cli_ml_train_insufficient(project):
    features.write_rows(synthetic_features(1, 30, signal=False))
    r = CliRunner().invoke(app, ["ml", "train"])
    assert r.exit_code == 0
    assert json.loads(r.stdout.strip().splitlines()[-1])["status"] == "insufficient_data"


def test_cli_ml_demote(project):
    _fast_ml_yaml(project)
    features.write_rows(synthetic_features(6, 320, signal=True))
    runner = CliRunner()
    out = json.loads(runner.invoke(app, ["ml", "train"]).stdout.strip().splitlines()[-1])
    assert out["promoted"] is True
    r = runner.invoke(app, ["ml", "demote", out["version"], "--reason", "검토"])
    assert r.exit_code == 0 and registry.latest("quant") is None


def test_cli_ml_train_horizon_override(project, monkeypatch):
    """`--horizon N` 은 설정 파일을 바꾸지 않고 그 실행의 label.horizon_days 만 덮어쓴다."""
    from trader.ml import train as train_mod

    seen = {}

    def fake_train(cfg, market=None, min_rows=100):
        seen["h"] = cfg.label.horizon_days
        return {"status": "trained", "promoted": False}

    monkeypatch.setattr(train_mod, "train_quant", fake_train)
    r = CliRunner().invoke(app, ["ml", "train", "--market", "KR", "--horizon", "10"])
    assert r.exit_code == 0, r.stdout
    assert seen["h"] == 10
    r = CliRunner().invoke(app, ["ml", "train", "--market", "KR"])
    assert seen["h"] == 5  # 기본은 ml.yaml 값


def test_cli_ml_train_horizon_rejects_invalid(project):
    for h in ("0", "-1", "61"):
        r = CliRunner().invoke(app, ["ml", "train", "--market", "KR", "--horizon", h])
        assert r.exit_code != 0 and "Traceback" not in (r.stdout + (r.stderr or "")), h


def test_cli_ml_tune_outputs_json(project, monkeypatch):
    from trader.ml import tune

    _fast_ml_yaml(project)
    features.write_rows(synthetic_features(6, 320, signal=True))
    monkeypatch.setitem(
        tune.GRIDS, "small", {"learning_rate": [0.05], "num_leaves": [7], "max_iter": [20]}
    )
    r = CliRunner().invoke(app, ["ml", "tune", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["status"] == "tuned" and out["best"]["params"]["num_leaves"] == 7
    assert CliRunner().invoke(app, ["ml", "tune", "--grid", "huge"]).exit_code != 0
