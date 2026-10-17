from datetime import UTC, date, datetime, timedelta

import yaml
from helpers_agents import ok_decision
from typer.testing import CliRunner

from trader.cli import app
from trader.ledger import db, repo
from trader.memory import store


def test_cli_reflect_fake(project, monkeypatch):
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
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    conn = db.connect()
    db.init_schema(conn)
    now = datetime.now(UTC)
    dec_id = repo.ingest_checkpoint(
        conn, date(2026, 10, 9), "KR", "005930", "30_decision", {"data": ok_decision()}
    )
    eid = store.record_decision(
        conn,
        decision_id=dec_id,
        ticker="005930",
        market="KR",
        account_id="paper-main",
        decided_at=(now - timedelta(days=8)).isoformat(),
        decision={"action": "BUY"},
        row=None,
        horizon_days=5,
    )
    store.fill_outcome(conn, eid, realized_return=0.02, alpha=0.0, outcome_at=now.isoformat())
    conn.close()
    r = CliRunner().invoke(app, ["reflect", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    assert "반성 1건" in r.stdout
    conn = db.connect()
    assert store.get_entry(conn, eid)["reflection"] is not None
    conn.close()
