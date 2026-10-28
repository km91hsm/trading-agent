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
    assert not (project / "state" / "external" / "saveticker" / "data" / "feed.sqlite3").exists()


def test_run_rejects_invalid_month_before_browser_start(project):
    result = CliRunner().invoke(
        app, ["saveticker", "run", "--once", "--start-month", "2027-01", "--end-month", "2026-01"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_operational_layout_separates_data_from_runtime(project):
    from trader.saveticker.cli import root, runtime

    assert root() == project / "state" / "external" / "saveticker" / "data"
    assert runtime() == project / "state" / "external" / "saveticker" / "runtime"


def test_sqlite_diagnostics_preserve_code_without_error_message():
    import sqlite3

    from trader.saveticker.cli import sqlite_diagnostics

    with sqlite3.connect(":memory:") as db:
        try:
            db.execute("SELECT private_secret_column")
        except sqlite3.Error as exc:
            result = sqlite_diagnostics(exc)
    assert result == {"sqlite_errorcode": 1, "sqlite_errorname": "SQLITE_ERROR"}
    assert "private_secret_column" not in str(result)


def test_plan_lanes_skips_paused_lanes():
    """서비스가 접근이 막힌 작업만 빼고 다시 띄운다(`run --skip-lanes community`)."""
    from trader.saveticker.cli import plan_lanes
    from trader.saveticker.service import View, build_views

    planned = build_views("2026-10", "2026-11") + [
        View("news_all", "news"),
        View("news_reports", "news"),
    ]
    assert plan_lanes(planned, None) == ["background", "community", "market", "news"]
    assert plan_lanes(planned, "community") == ["background", "market", "news"]
    assert plan_lanes(planned, "community, market") == ["background", "news"]


def test_lane_login_is_serialized_with_shared_lock(tmp_path):
    """네 작업이 동시에 로그인하지 않게 같은 잠금 파일로 한 번에 하나씩."""
    from trader.platform import filelock
    from trader.saveticker.cli import serialized_login

    held = []

    class Adapter:
        def login(self):
            fh = filelock.try_hold(tmp_path / "login.lock")
            held.append(fh is None)  # 로그인 중에는 다른 작업이 잠금을 못 잡는다
            if fh is not None:
                filelock.release_hold(fh)

    serialized_login(Adapter(), tmp_path)
    assert held == [True]
    fh = filelock.try_hold(tmp_path / "login.lock")
    assert fh is not None  # 끝나면 풀린다
    filelock.release_hold(fh)
