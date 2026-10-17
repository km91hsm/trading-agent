import json

from helpers_memory import seed_matured
from typer.testing import CliRunner

from trader.cli import app
from trader.ledger import db


def test_cli_calibration_build(project):
    conn = db.connect()
    db.init_schema(conn)
    seed_matured(conn, 60)
    conn.close()
    r = CliRunner().invoke(app, ["calibration", "build"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["active"] is True and out["n_decisions"] == 60
