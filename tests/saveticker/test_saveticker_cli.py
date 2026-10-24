import json

from typer.testing import CliRunner

from trader.cli import app


def test_import_and_status_and_query_need_no_browser_or_broker(project):
    p = project / "capture.json"
    p.write_text(
        json.dumps(
            {
                "stream": "news_all",
                "page_url": "https://www.saveticker.com/news",
                "observed_at": "2026-10-25T00:00:00Z",
                "items": [{"id": "x", "title": "속보", "tickers": ["AAPL"]}],
            }
        )
    )
    runner = CliRunner()
    imported = runner.invoke(app, ["saveticker", "import-capture", str(p)])
    assert imported.exit_code == 0, imported.output
    status = runner.invoke(app, ["saveticker", "status"])
    assert status.exit_code == 0 and json.loads(status.output)["streams"]["news_all"]["count"] == 1
    q = runner.invoke(
        app, ["saveticker", "query", "--asof", "2026-10-25T00:01:00Z", "--ticker", "AAPL"]
    )
    assert q.exit_code == 0 and json.loads(q.output)[0]["item"]["title"] == "속보"
    assert not (project / "state" / "trader.db").exists()


def test_invalid_capture_does_not_echo_input_secrets(project):
    p = project / "capture.json"
    p.write_text('{"password":"NEVER_ECHO_THIS"}')
    result = CliRunner().invoke(app, ["saveticker", "import-capture", str(p)])
    assert result.exit_code == 1
    assert "NEVER_ECHO_THIS" not in result.output


def test_status_without_data_does_not_create_database(project):
    result = CliRunner().invoke(app, ["saveticker", "status"])
    assert result.exit_code == 0
    assert json.loads(result.output)["streams"] == {}
    assert not (project / "state" / "external" / "saveticker" / "feed.sqlite3").exists()


def test_run_rejects_invalid_month_before_browser_start(project):
    result = CliRunner().invoke(
        app, ["saveticker", "run", "--once", "--start-month", "2027-01", "--end-month", "2026-01"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output
