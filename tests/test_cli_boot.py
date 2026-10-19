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
