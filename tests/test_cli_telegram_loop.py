import yaml
from conftest import make_account
from typer.testing import CliRunner

from trader import cli
from trader.broker.fake import FakeBroker
from trader.cli import app
from trader.notify.telegram import Update


class FakeReceiver:
    def __init__(self):
        self.n = 0

    def poll(self, offset=None):
        self.n += 1
        return [Update(update_id=self.n, text="/status", chat_id="1")]


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def test_cli_telegram_loop_runs_n_times(project, monkeypatch):
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
    recv = FakeReceiver()
    sent = Sent()
    monkeypatch.setattr(cli, "make_receiver", lambda s: recv)
    monkeypatch.setattr(cli, "make_notifier", lambda s: sent)
    monkeypatch.setattr(
        cli,
        "_build_brokers",
        lambda s, a: [FakeBroker(make_account("paper-main", "paper", "auto"))],
    )
    r = CliRunner().invoke(app, ["telegram", "loop", "--interval", "0", "--max-iterations", "2"])
    assert r.exit_code == 0, r.stdout
    assert recv.n == 2 and len(sent.msgs) == 2 and r.stdout.count("status") == 2
