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
