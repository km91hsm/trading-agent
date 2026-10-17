import pytest
import yaml

from trader import config


def _write(project, data):
    p = project / "config" / "schedule.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


BASE = {
    "kr": {
        "scan": "07:30",
        "analyze": "07:40",
        "monitor": {"start": "09:00", "end": "15:30", "interval_s": 60},
    },
    "us": {
        "scan": "21:30",
        "analyze": "21:40",
        "monitor": {"start": "23:30", "end": "06:00", "interval_s": 60},
    },
    "reflect": "16:30",
    "ml_train": {"weekday": "sat", "time": "10:00"},
}


def test_load_schedule_parses_and_validates(project):
    _write(project, BASE)
    s = config.load_schedule()
    assert s.kr.monitor.interval_s == 60 and s.us.monitor.end == "06:00"
    assert s.ml_train.weekday == "sat" and s.reflect == "16:30"
    assert s.kr.scan_hm() == (7, 30) and s.us.monitor.start_hm() == (23, 30)


def test_load_schedule_rejects_bad_time_and_unknown_key(project):
    bad = dict(BASE, reflect="25:00")
    _write(project, bad)
    with pytest.raises(config.ConfigError):
        config.load_schedule()
    bad = dict(BASE, extra=1)
    _write(project, bad)
    with pytest.raises(config.ConfigError):
        config.load_schedule()
    bad = dict(BASE, ml_train={"weekday": "someday", "time": "10:00"})
    _write(project, bad)
    with pytest.raises(config.ConfigError):
        config.load_schedule()


def test_load_schedule_missing_file_is_error(project):
    with pytest.raises(config.ConfigError):
        config.load_schedule()


def test_repo_schedule_yaml_loads(project):
    import shutil
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "config" / "schedule.yaml"
    shutil.copy(src, project / "config" / "schedule.yaml")
    s = config.load_schedule()
    assert s.kr.monitor.start == "09:00"


def test_channels_config_defaults(project):
    (project / "config" / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "edgar": {"user_agent": "t t@example.com"},
                "channels": {"youtube": ["UCabc"], "telegram": ["chan"]},
            }
        ),
        encoding="utf-8",
    )
    c = config.load_sources().channels
    assert c.youtube == ["UCabc"] and c.telegram == ["chan"]
    assert c.max_videos == 3 and c.max_posts == 30


def test_market_schedule_tz_optional(project):
    data = dict(BASE)
    data["us"] = {**BASE["us"], "tz": "America/New_York"}
    _write(project, data)
    s = config.load_schedule()
    assert s.us.tz == "America/New_York" and s.kr.tz == "Asia/Seoul"
    data["us"]["tz"] = "Mars/Olympus"
    _write(project, data)
    with pytest.raises(config.ConfigError):
        config.load_schedule()
