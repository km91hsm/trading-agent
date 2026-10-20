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


def test_rl_sizer_defaults_and_mode(project):
    import pytest
    from pydantic import ValidationError

    rl = config.MlConfig().models.rl_sizer
    assert rl.enabled is False and rl.seeds == 3 and rl.mode == "shadow"
    assert rl.backend == "auto" and rl.max_weight == 0.25 and rl.min_steps == 300
    with pytest.raises(ValidationError):
        config.MlConfig.model_validate({"models": {"rl_sizer": {"mode": "live"}}})
    assert config.load_ml().models.rl_sizer.mode == "shadow"  # config/ml.yaml 과 모델이 맞는다


def test_label_mode_default_absolute_and_relative_allowed():
    from trader.config import MlConfig

    assert MlConfig().label.mode == "absolute"
    cfg = MlConfig.model_validate({"label": {"mode": "relative_tercile"}})
    assert cfg.label.mode == "relative_tercile"


def test_load_sectors_covers_universe():
    """config/sectors.yaml 은 유니버스 전 종목을 11개 대분류 중 하나로 매핑한다."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    sectors = config.load_sectors(repo / "config" / "sectors.yaml")
    universe = config.load_universe(repo / "config" / "universe.yaml")
    assert len(sectors.sectors) == 11 and "기타" in sectors.sectors
    assert set(universe.kr) <= set(sectors.kr)
    assert {s.symbol for s in universe.us} <= set(sectors.us)
    assert set(sectors.kr.values()) | set(sectors.us.values()) <= set(sectors.sectors)
    assert sectors.sector_of("005930", "KR") == "반도체"
    assert sectors.sector_of("JPM", "US") == "금융"
    assert sectors.sector_of("005930", "US") is None


def test_load_sectors_missing_file_is_empty(project):
    s = config.load_sectors()
    assert s.kr == {} and s.us == {} and s.sector_of("005930", "KR") is None


def test_sectors_values_must_be_declared(project):
    (project / "config" / "sectors.yaml").write_text(
        "sectors: [반도체]\nkr: {'005930': 금융}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError):
        config.load_sectors()
