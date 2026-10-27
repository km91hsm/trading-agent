"""같은 명의의 계좌 여럿(KIS 는 앱키를 계좌마다 발급): 계좌 전용 키 `KIS_<ID>_*` 가 있으면 그것,
없으면 env 공용 키. 토큰 캐시·레이트리미터는 앱키 단위, 체결통보는 HTS ID 단위로 하나."""

import logging

import pytest

from trader import cli, config
from trader.config import AccountConfig, Secrets


def _acct(id_, env="real", **kw):
    return AccountConfig(id=id_, env=env, cano="00000000", mode="off", **kw)


SECRETS = Secrets.model_validate(
    {
        "KIS_REAL_APP_KEY": "shared-k",
        "KIS_REAL_APP_SECRET": "shared-s",
        "KIS_REAL_CANO": "11111111",
        "KIS_HTS_ID": "me",
        "KIS_REAL_2_APP_KEY": "k2",
        "KIS_REAL_2_APP_SECRET": "s2",
        "KIS_REAL_2_CANO": "22222222",
        "KIS_ISA_APP_KEY": "k3",
        "KIS_ISA_APP_SECRET": "s3",
    }
)


def test_creds_prefer_account_keys_then_shared():
    main = SECRETS.kis_creds(_acct("real-main"))
    assert main.shared and main.app_key == "shared-k"
    assert main.bucket == "real-" + config.key_fingerprint("shared-k")
    two = SECRETS.kis_creds(_acct("real-2"))
    assert not two.shared and two.app_key == "k2" and two.cano == "22222222"
    assert two.hts_id == "me"  # 전용 HTS ID 가 없으면 공용
    # 같은 앱키를 다른 접두사로 넣어도 같은 버킷(앱키 지문)
    same = Secrets.model_validate({"KIS_A_APP_KEY": "k2", "KIS_A_APP_SECRET": "s2"})
    assert same.kis_creds(_acct("a")).bucket == two.bucket != main.bucket
    isa = SECRETS.kis_creds(_acct("isa-1", credentials="KIS_ISA_"))
    assert isa.app_key == "k3" and isa.prefix == "KIS_ISA_"
    assert SECRETS.kis_creds(env="paper").app_key is None


def test_bad_credentials_prefix_rejected():
    with pytest.raises(ValueError):
        _acct("x", credentials="kis_lower_")


def test_placeholder_cano_filled_from_account_prefix(project):
    (project / "config" / "accounts.yaml").write_text(
        "accounts:\n"
        '  - {id: real-main, env: real, cano: "00000000", mode: "off"}\n'
        '  - {id: real-2, env: real, cano: "00000000", mode: "off"}\n',
        encoding="utf-8",
    )
    accts = config.load_accounts(secrets=SECRETS)
    assert [a.cano for a in accts] == ["11111111", "22222222"]
    # 전용 키만 있고 전용 계좌번호가 없으면 공용 번호를 빌리지 않는다(자리표시자 그대로)
    no_cano = Secrets.model_validate({**SECRETS.model_dump(), "KIS_REAL_2_CANO": None})
    assert config.load_accounts(secrets=no_cano)[1].cano == "00000000"


def test_env_var_overrides_account_key(project, monkeypatch):
    (project / "secrets" / ".env").write_text("KIS_REAL_2_APP_KEY=file\n", encoding="utf-8")
    monkeypatch.setenv("KIS_REAL_2_APP_KEY", "env")
    assert config.load_secrets().get("KIS_REAL_2_APP_KEY") == "env"


def test_brokers_use_per_key_clients_and_warn_on_shared_key(project, caplog):
    cli._clients.clear()
    accts = [_acct("real-main"), _acct("real-2"), _acct("real-3")]
    with caplog.at_level(logging.WARNING, logger="trader.kis"):
        brokers = {b.account.id: b for b in cli._build_brokers(SECRETS, accts)}
    assert brokers["real-2"].client is not brokers["real-main"].client
    fp = config.key_fingerprint("k2")
    assert brokers["real-2"].client.auth.cache_path.name == f"kis_token_real-{fp}.json"
    assert brokers["real-3"].client is brokers["real-main"].client  # 전용 키 없음 → 공용
    assert any("real-3" in r.message and "KIS_REAL_3_" in r.message for r in caplog.records)
    cli._clients.clear()


def test_market_falls_back_to_first_real_account_key():
    only = Secrets.model_validate({"KIS_REAL_2_APP_KEY": "k2", "KIS_REAL_2_APP_SECRET": "s2"})
    assert cli._market_creds(only, [_acct("real-2")]).app_key == "k2"
    assert cli._market_creds(SECRETS, [_acct("real-2")]).app_key == "shared-k"


def test_one_notice_listener_per_hts_id_prefers_non_market_key(project, conn):
    from trader.ledger import db

    cli._clients.clear()
    settings = config.Settings()
    accts = [_acct("real-main"), _acct("real-2")]
    lst = cli._notice_listeners(settings, SECRETS, accts, db.connect)
    assert len(lst) == 1 and lst[0].env == "real"
    # 시세 허브(공용 키)와 겹치지 않는 real-2 키로 붙는다 → 허브가 비켜설 필요 없음
    assert lst[0].shares_market_session is False
    only_shared = cli._notice_listeners(settings, SECRETS, [_acct("real-main")], db.connect)
    assert only_shared[0].shares_market_session is True
    cli._clients.clear()


def test_token_cache_ignores_token_of_other_app_key(project):
    import json
    from datetime import UTC, datetime, timedelta

    from trader.broker.kis_auth import KisAuth

    old = KisAuth("real", "old-key", "s", name="real")
    exp = (datetime.now(UTC) + timedelta(hours=10)).isoformat()
    payload = {"access_token": "OLD", "expires_at": exp, "key": config.key_fingerprint("old-key")}
    old.cache_path.write_text(json.dumps(payload), encoding="utf-8")
    assert old._read_cache().get("token") == "OLD"
    new = KisAuth("real", "new-key", "s", name="real")  # 같은 파일, 키만 바뀜
    assert new._read_cache() == {}
