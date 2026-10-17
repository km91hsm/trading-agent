import yaml
from typer.testing import CliRunner

from trader.cli import app
from trader.control import control
from trader.ledger import db


def _accounts(project):
    data = {
        "accounts": [
            {"id": "paper-main", "env": "paper", "cano": "0", "markets": ["KR"], "mode": "auto"},
            {"id": "real-main", "env": "real", "cano": "1", "markets": ["KR"], "mode": "off"},
        ]
    }
    (project / "config" / "accounts.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_control_mode_sets_and_shows(project):
    _accounts(project)
    runner = CliRunner()
    r = runner.invoke(app, ["control", "mode", "paper-main", "approve"])
    assert r.exit_code == 0, r.stdout
    conn = db.connect()
    assert control.get_flag(conn, "mode:paper-main") == "approve"
    conn.close()
    r = runner.invoke(app, ["control", "show"])
    assert r.exit_code == 0 and "paper-main" in r.stdout and "approve" in r.stdout
    assert "일시정지" in r.stdout


def test_control_mode_real_auto_requires_confirm(project):
    _accounts(project)
    runner = CliRunner()
    r = runner.invoke(app, ["control", "mode", "real-main", "auto"])
    assert r.exit_code == 1 and "LIVE" in (r.stdout + (r.stderr or ""))
    conn = db.connect()
    db.init_schema(conn)
    assert control.get_flag(conn, "mode:real-main") is None
    conn.close()
    r = runner.invoke(app, ["control", "mode", "real-main", "auto", "--confirm", "LIVE"])
    assert r.exit_code == 0
    conn = db.connect()
    assert control.get_flag(conn, "live_confirmed:real-main")
    conn.close()


def test_control_mode_rejects_unknown(project):
    _accounts(project)
    runner = CliRunner()
    assert runner.invoke(app, ["control", "mode", "nope", "auto"]).exit_code == 1
    assert runner.invoke(app, ["control", "mode", "paper-main", "fast"]).exit_code == 1


def test_control_pause_resume(project):
    _accounts(project)
    runner = CliRunner()
    assert runner.invoke(app, ["control", "pause"]).exit_code == 0
    conn = db.connect()
    assert control.is_paused(conn)
    conn.close()
    assert runner.invoke(app, ["control", "resume"]).exit_code == 0
    conn = db.connect()
    assert not control.is_paused(conn)
    conn.close()
