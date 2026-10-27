"""CLI 부트스트랩 헬퍼: 로깅·설정·계좌·시크릿·원장 연결을 한 곳에서 묶는다 (명령마다 반복 4~6줄)."""

import yaml

from trader import cli
from trader.ledger import db


def test_boot_loads_requested_parts_only(project):
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": "paper-main",
                        "env": "paper",
                        "cano": "0",
                        "markets": ["KR"],
                        "mode": "auto",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    b = cli._boot(accounts=True, conn=True)
    try:
        assert [a.id for a in b.accounts] == ["paper-main"]
        assert db.current_version(b.conn) == db.SCHEMA_VERSION  # init_schema 까지 끝난 연결
        assert b.settings is None and b.secrets is None
    finally:
        b.conn.close()
    b2 = cli._boot(settings=True, secrets=True)
    assert b2.settings is not None and b2.secrets is not None and b2.conn is None
    assert (project / "state" / "logs" / "trader.log").exists()  # configure 도 함께


def test_boot_three_lists(project):
    """Phase 26: fo 계좌는 `accounts`(현물)에 없고 `fo_accounts` 에, `all_accounts` 는 순서 유지."""
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": "paper-main",
                        "env": "paper",
                        "cano": "0",
                        "markets": ["KR"],
                        "mode": "auto",
                    },
                    {
                        "id": "paper-fo",
                        "env": "paper",
                        "cano": "0",
                        "acnt_prdt_cd": "03",
                        "markets": ["KR"],
                        "mode": "auto",
                        "asset": "fo",
                    },
                    {
                        "id": "real-main",
                        "env": "real",
                        "cano": "0",
                        "markets": ["KR"],
                        "mode": "off",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    b = cli._boot(accounts=True)
    assert [a.id for a in b.accounts] == ["paper-main", "real-main"]
    assert [a.id for a in b.fo_accounts] == ["paper-fo"]
    assert [a.id for a in b.all_accounts] == ["paper-main", "paper-fo", "real-main"]
    b2 = cli._boot(settings=True)
    assert b2.accounts is None and b2.fo_accounts is None and b2.all_accounts is None
