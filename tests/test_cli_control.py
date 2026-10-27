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


def test_control_exec_key_list_and_release(project):
    """전체 검토 2차 2-O13: 보낸 뒤 예외로 영구 선점된 실행 키를 CLI 로 보고 푼다(메모 필수)."""
    from trader.ledger import repo

    _accounts(project)
    conn = db.connect()
    db.init_schema(conn)
    repo.claim_exec_key(conn, "7|paper-main|BUY|50_order", now="t0", stale_before="t0")
    repo.mark_exec_key_sent(conn, "7|paper-main|BUY|50_order", sent_at="t1")
    conn.close()
    runner = CliRunner()
    r = runner.invoke(app, ["control", "exec-key", "list"])
    assert r.exit_code == 0 and "7|paper-main|BUY|50_order" in r.stdout and "t1" in r.stdout
    r = runner.invoke(app, ["control", "exec-key", "release", "7|paper-main|BUY|50_order"])
    assert r.exit_code == 1  # 메모 없이는 풀지 않는다
    r = runner.invoke(
        app,
        [
            "control",
            "exec-key",
            "release",
            "7|paper-main|BUY|50_order",
            "--note",
            "KIS 주문 없음 확인",
        ],
    )
    assert r.exit_code == 0, r.stdout
    conn = db.connect()
    assert repo.list_exec_keys(conn) == []
    assert control.get_flag(conn, "exec_key_released:7|paper-main|BUY|50_order")["note"]
    conn.close()
    r = runner.invoke(app, ["control", "exec-key", "release", "none", "--note", "메모 열 자 이상"])
    assert r.exit_code == 1


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


def test_control_show_reports_effective_mode(project):
    _accounts(project)
    conn = db.connect()
    db.init_schema(conn)
    control.set_flag(conn, "mode:real-main", "auto")  # 확인 기록 없음
    conn.close()
    r = CliRunner().invoke(app, ["control", "show"])
    assert r.exit_code == 0, r.stdout
    line = next(x for x in r.stdout.splitlines() if x.startswith("real-main"))
    assert "off" in line and "live_confirmed" in line


def test_control_pause_reports_config_error_as_one_line(project):
    """설정 오류는 트레이스백이 아니라 '오류: …' 한 줄 + exit 1 (`_config_errors` 미적용 명령)."""
    (project / "config" / "accounts.yaml").write_text("accounts: [{id: x}]", encoding="utf-8")
    r = CliRunner().invoke(app, ["control", "pause"])
    assert r.exit_code == 1 and "오류:" in (r.stdout + (r.stderr or ""))
    assert "Traceback" not in (r.stdout + (r.stderr or ""))
    r = CliRunner().invoke(app, ["features", "snapshot", "--asof", "2026/10/18"])
    assert r.exit_code == 1 and "오류:" in (r.stdout + (r.stderr or ""))
