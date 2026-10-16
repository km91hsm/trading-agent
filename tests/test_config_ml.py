import pytest
import yaml

from trader import config


def test_load_ml_defaults_and_repo_yaml(project):
    m = config.load_ml()  # 파일 없으면 기본값
    assert m.label.horizon_days == 5 and m.label.thresholds.up == 0.02
    assert m.label.thresholds.down == -0.02 and m.label.embargo_days == 5
    assert m.train.walk_forward_folds == 6 and m.models.quant.promote_min_accuracy == 0.40
    import shutil
    from pathlib import Path

    shutil.copy(
        Path(__file__).resolve().parents[1] / "config" / "ml.yaml", project / "config" / "ml.yaml"
    )
    m2 = config.load_ml()
    assert m2.models.calibration.min_decisions == 60 and m2.models.rl_sizer.seeds == 3
    (project / "config" / "ml.yaml").write_text(
        yaml.safe_dump({"label": {"horizon": 3}}), encoding="utf-8"
    )
    with pytest.raises(config.ConfigError):
        config.load_ml()
