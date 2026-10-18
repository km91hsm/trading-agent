from decimal import Decimal

import pytest
import yaml

from trader import config


def acc(id, env, mode, markets=("KR", "US"), **extra):
    d = {
        "id": id,
        "env": env,
        "cano": "00000000",
        "acnt_prdt_cd": "01",
        "markets": list(markets),
        "mode": mode,
    }
    d.update(extra)
    return d


def write_yaml(project, name, data):
    (project / "config" / name).write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def test_defaults_load(project):
    s = config.load_settings()
    assert s.risk.max_position_pct == 0.25 and s.risk.max_positions == 5
    assert s.debate_rounds == 1 and s.anonymize is False
    assert s.llm.daily_session_cap == 12
    assert s.llm.budget_usd == 5.0  # 실기: 전문 전달 세션이 $2 를 넘겨 중단됐다
    assert s.web.port == 8787 and s.markets == ["KR", "US"]


def test_unknown_key_is_config_error(project):
    write_yaml(project, "settings.yaml", {"foo": 1})
    with pytest.raises(config.ConfigError, match="foo"):
        config.load_settings()


def test_debate_rounds_range(project):
    write_yaml(project, "settings.yaml", {"debate_rounds": 3})
    with pytest.raises(config.ConfigError, match="debate_rounds"):
        config.load_settings()


def test_accounts_load_and_initial_capital(project):
    write_yaml(
        project,
        "accounts.yaml",
        {"accounts": [acc("p", "paper", "auto", initial_capital="10000000")]},
    )
    accounts = config.load_accounts()
    assert accounts[0].initial_capital == Decimal("10000000")
    assert accounts[0].acnt_prdt_cd == "01"


def test_accounts_unique_ids(project):
    write_yaml(
        project,
        "accounts.yaml",
        {"accounts": [acc("a", "paper", "auto"), acc("a", "paper", "auto")]},
    )
    with pytest.raises(config.ConfigError, match="중복"):
        config.load_accounts()


def test_real_account_auto_in_yaml_rejected(project):
    write_yaml(project, "accounts.yaml", {"accounts": [acc("r", "real", "auto")]})
    with pytest.raises(config.ConfigError, match="실전"):
        config.load_accounts()


def test_accounts_missing_file_is_empty(project):
    assert config.load_accounts() == []


def test_sources_defaults_follow_policy(project):
    write_yaml(project, "sources.yaml", {"edgar": {"user_agent": "trader test@example.com"}})
    s = config.load_sources()
    assert s.community.reddit is True and s.community.clien is True
    assert s.community.dcinside is False and s.community.fmkorea is False
    assert s.community.toss is False and s.community.naver_board is False
    assert s.news.google_rss is True and s.channels.youtube == []


def test_sources_requires_user_agent(project):
    with pytest.raises(config.ConfigError, match="edgar"):
        config.load_sources()


def test_universe_load(project):
    write_yaml(
        project,
        "universe.yaml",
        {
            "kr": ["005930"],
            "us": [{"symbol": "AAPL", "exchange": "NAS"}],
            "exclude": [],
            "watch": [],
        },
    )
    u = config.load_universe()
    assert u.kr == ["005930"] and u.us[0].exchange == "NAS"


def test_universe_bad_exchange(project):
    write_yaml(project, "universe.yaml", {"kr": [], "us": [{"symbol": "X", "exchange": "LSE"}]})
    with pytest.raises(config.ConfigError):
        config.load_universe()


def test_secrets_from_env_when_file_missing(project, monkeypatch):
    monkeypatch.setenv("KIS_PAPER_APP_KEY", "k")
    assert config.load_secrets().KIS_PAPER_APP_KEY == "k"


def test_env_overrides_file(project, monkeypatch):
    (project / "secrets" / ".env").write_text(
        "DART_API_KEY=file\nWEB_TOKEN=file\n", encoding="utf-8"
    )
    monkeypatch.setenv("DART_API_KEY", "env")
    monkeypatch.setenv("WEB_TOKEN", "")
    s = config.load_secrets()
    assert s.DART_API_KEY == "env"
    assert s.WEB_TOKEN == "file"


def test_cp949_yaml_is_config_error(project):
    raw = "# 한글 주석\ndebate_rounds: 1\n".encode("cp949")
    (project / "config" / "settings.yaml").write_bytes(raw)
    with pytest.raises(config.ConfigError, match="UTF-8"):
        config.load_settings()


def test_cp949_env_is_config_error(project):
    (project / "secrets" / ".env").write_bytes("# 한글\nDART_API_KEY=x\n".encode("cp949"))
    with pytest.raises(config.ConfigError, match="UTF-8"):
        config.load_secrets()


def test_whitespace_env_does_not_override(project, monkeypatch):
    (project / "secrets" / ".env").write_text("DART_API_KEY=file\n", encoding="utf-8")
    monkeypatch.setenv("DART_API_KEY", "   ")
    assert config.load_secrets().DART_API_KEY == "file"


def test_bad_yaml_is_config_error(project):
    (project / "config" / "settings.yaml").write_text("a: [", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_repo_config_files_load(project, monkeypatch):
    """저장소의 실제 config/*.yaml 이 로더를 통과한다 (accounts 는 값 미출력)."""
    import shutil
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1] / "config"
    for f in repo.glob("*.yaml"):
        shutil.copy(f, project / "config" / f.name)
    assert config.load_settings().models["trader"] == "opus"
    assert len(config.load_universe().kr) >= 10
    assert config.load_sources().edgar.user_agent
    ids = [a.id for a in config.load_accounts()]
    assert len(ids) == len(set(ids))
