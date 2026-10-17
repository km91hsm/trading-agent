"""`trader approvals …`·`trader telegram poll` (브로커·알림·수신기는 가짜로 바꿔 끼운다)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import yaml
from conftest import make_account
from typer.testing import CliRunner

from trader import cli
from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.cli import app
from trader.control import control
from trader.ledger import db, repo
from trader.notify.telegram import Update
from trader.orchestrator import executor

D = date(2026, 10, 9)
T0 = datetime.now(UTC)  # CLI 는 실제 시각으로 만료를 판정하므로 요청 시각도 현재로


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


class FakeReceiver:
    def __init__(self, updates):
        self.updates = updates
        self.offsets = []

    def poll(self, offset=None):
        self.offsets.append(offset)
        return [u for u in self.updates if offset is None or u.update_id >= offset]


def _setup(project, monkeypatch):
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": "paper-main",
                        "env": "paper",
                        "cano": "0",
                        "markets": ["KR"],
                        "mode": "approve",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    acct = make_account("paper-main", "paper", "approve")
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    broker = FakeBroker(acct, cash=cash)
    sent = Sent()
    monkeypatch.setattr(cli, "_build_brokers", lambda s, accounts: [broker])
    monkeypatch.setattr(cli, "make_notifier", lambda s: sent)
    conn = db.connect()
    db.init_schema(conn)
    ectx = executor.ExecContext(
        settings=cli.config.load_settings(),
        accounts=[acct],
        conn=conn,
        brokers={"paper-main": broker},
        notifier=sent,
        now=lambda: T0,
    )
    from helpers_agents import ok_decision

    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": dec.model_dump()})
    res = executor.execute(ectx, dec, date=D, market="KR", price=Decimal("71500"), dry_run=False)
    aid = res[0]["approval_id"]
    conn.close()
    return broker, sent, aid


def test_cli_approvals_flow(project, monkeypatch):
    broker, sent, aid = _setup(project, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(app, ["approvals", "list"])
    assert r.exit_code == 0 and f"#{aid}" in r.stdout and "005930" in r.stdout
    r = runner.invoke(app, ["approvals", "approve", str(aid)])
    assert r.exit_code == 0 and "FAKE-1" in r.stdout and len(broker.orders) == 1
    r = runner.invoke(app, ["approvals", "approve", str(aid)])
    assert r.exit_code == 1  # 이미 처리됨
    r = runner.invoke(app, ["approvals", "reject", "999"])
    assert r.exit_code == 1


def test_cli_telegram_poll_processes_commands(project, monkeypatch):
    broker, sent, aid = _setup(project, monkeypatch)
    receiver = FakeReceiver(
        [
            Update(update_id=5, text="/status", chat_id="1"),
            Update(update_id=6, text=f"/reject {aid}", chat_id="1"),
            Update(update_id=7, text="잡담", chat_id="1"),
        ]
    )
    monkeypatch.setattr(cli, "make_receiver", lambda s: receiver)
    runner = CliRunner()
    r = runner.invoke(app, ["telegram", "poll"])
    assert r.exit_code == 0, r.stdout
    assert "reject" in r.stdout and broker.orders == []
    conn = db.connect()
    assert repo.get_approval(conn, aid)["status"] == "rejected"
    assert control.get_flag(conn, "telegram_offset") == 8
    conn.close()
    assert any("미승인" in m or "상태" in m for m in sent.msgs)  # /status 응답
    # 두 번째 폴링은 저장된 오프셋부터
    runner.invoke(app, ["telegram", "poll"])
    assert receiver.offsets[-1] == 8


def test_cli_telegram_poll_without_config_fails(project, monkeypatch):
    _setup(project, monkeypatch)
    monkeypatch.setattr(cli, "make_receiver", lambda s: None)
    r = CliRunner().invoke(app, ["telegram", "poll"])
    assert r.exit_code == 1
