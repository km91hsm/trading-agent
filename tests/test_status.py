from datetime import UTC, datetime

from conftest import make_account

from trader import status
from trader.config import Settings
from trader.ledger import db

TODO = """# 체크리스트
## Phase 0 골격
- [x] 하나
- [ ] 둘
- [ ] 완료 기준: 셋
## Phase 1 브로커
- [ ] 넷
"""


def test_todo_progress_counts(project):
    p = project / "docs" / "TODO.md"
    p.write_text(TODO, encoding="utf-8")
    assert status.todo_progress(p) == {"Phase 0 골격": (1, 3), "Phase 1 브로커": (0, 1)}


def test_todo_progress_missing_file(project):
    assert status.todo_progress(project / "docs" / "없음.md") == {}


def test_build_status_sections(conn, project):
    p = project / "docs" / "TODO.md"
    p.write_text(TODO, encoding="utf-8")
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    rid = db.start_run(conn, "analyze", "KR")
    db.finish_run(conn, rid, "ok")
    conn.execute(
        "INSERT INTO agent_calls(agent, status, created_at) VALUES ('orchestrator', 'ok', ?)",
        (db.now_iso(),),
    )
    conn.commit()
    now = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
    text = status.build_status(conn, Settings(), accounts, p, now=now)
    assert text.startswith("# 시스템 상태 (생성: 2026-10-09 09:00 KST)")
    assert "| paper-main | paper | auto | KR |" in text
    assert "| real-main | real | off | KR |" in text
    assert "analyze KR" in text and "→ ok" in text
    assert "Phase 0 골격: 1/3" in text
    assert "- 둘" in text
    assert "일시정지: 아니오" in text
    assert len(text) <= status.MAX_CHARS


def test_build_status_truncates(conn, project):
    p = project / "docs" / "TODO.md"
    p.write_text("## Phase 0 x\n" + "- [ ] " + "가" * 9000 + "\n", encoding="utf-8")
    text = status.build_status(conn, Settings(), [], p)
    assert len(text) <= status.MAX_CHARS


def test_write_status_default_path(project):
    out = status.write_status("안녕\n")
    assert out == project / "state" / "STATUS.md" and out.read_text(encoding="utf-8") == "안녕\n"
