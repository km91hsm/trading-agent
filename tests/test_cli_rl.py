import json

from helpers_ml import synthetic_features
from typer.testing import CliRunner

from trader.cli import app
from trader.ml import features, registry
from trader.ml.rl.policy import FixedPolicy

runner = CliRunner()


def test_rl_train_models_promote_demote(project, monkeypatch):
    features.write_rows(synthetic_features(3, 400, signal=True))
    monkeypatch.setattr(
        "trader.ml.rl.learners.fit",
        lambda s, c, *, seed, backend=None: (FixedPolicy(0.1), "fake"),
    )
    out = runner.invoke(app, ["rl", "train", "--seeds", "1"])
    assert out.exit_code == 0, out.stdout
    v = json.loads(out.stdout.strip().splitlines()[-1])["version"]
    assert "미승격" in runner.invoke(app, ["rl", "models"]).stdout
    assert runner.invoke(app, ["rl", "promote", v]).exit_code == 0
    assert registry.latest("rl_sizer") is not None
    assert runner.invoke(app, ["rl", "demote", v]).exit_code == 0
    assert registry.latest("rl_sizer") is None
    assert runner.invoke(app, ["rl", "promote", "19990101_000000"]).exit_code == 1


def test_rl_train_seeds_range(project):
    assert runner.invoke(app, ["rl", "train", "--seeds", "0"]).exit_code == 2
    assert runner.invoke(app, ["rl", "train", "--seeds", "11"]).exit_code == 2
