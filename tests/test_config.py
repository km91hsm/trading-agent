from decimal import Decimal

import pydantic
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
    assert s.llm.daily_session_cap == 80 and s.llm.market_session_cap == 2
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


def test_universe_and_sectors_memo_by_file_mtime(project):
    """전체 검토 3차(성능): 같은 파일이면 같은(frozen) 객체, 파일이 바뀌면 다시 읽는다."""
    import os

    write_yaml(project, "universe.yaml", {"kr": ["005930"], "us": []})
    a = config.load_universe()
    assert config.load_universe() is a
    with pytest.raises(pydantic.ValidationError):
        a.kr = ["000660"]  # frozen — 나눠 쓰는 객체를 바꾸지 못한다
    path = project / "config" / "universe.yaml"
    write_yaml(project, "universe.yaml", {"kr": ["000660"], "us": []})
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert config.load_universe().kr == ["000660"]
    s1 = config.load_sectors()
    assert config.load_sectors() is s1


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


def test_accounts_cano_placeholder_filled_from_secrets(project):
    """accounts.yaml 의 cano 가 "00000000" 이면 secrets 의 KIS_<ENV>_CANO 로 채운다
    (공개 저장소 대비)."""
    (project / "config" / "accounts.yaml").write_text(
        'accounts:\n  - {id: p, env: paper, cano: "00000000", markets: [KR], mode: auto}\n'
        '  - {id: r, env: real, cano: "12345678", markets: [KR], mode: "off"}\n'
        '  - {id: r2, env: real, cano: "00000000", markets: [KR], mode: "off"}\n',
        encoding="utf-8",
    )
    secrets = config.Secrets(KIS_PAPER_CANO="11112222")
    accts = {a.id: a for a in config.load_accounts(secrets=secrets)}
    assert accts["p"].cano == "11112222"
    assert accts["r"].cano == "12345678"  # 실제 값은 그대로
    assert accts["r2"].cano == "00000000"  # env 도 비면 자리표시자 유지(경고만)
