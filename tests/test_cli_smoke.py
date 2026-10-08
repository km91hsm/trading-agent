from typer.testing import CliRunner

from trader.cli import app


def test_help_lists_status():
    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "status" in r.stdout
