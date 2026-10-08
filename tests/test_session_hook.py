import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "session_start.py"


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={"CLAUDE_PROJECT_DIR": str(root), "PATH": "", "PYTHONIOENCODING": "cp949"},
        shell=False,
    )


def test_hook_prints_status_and_todos(project):
    (project / "state" / "STATUS.md").write_text(
        "# 시스템 상태\n- 일시정지: 아니오\n", encoding="utf-8"
    )
    (project / "docs" / "TODO.md").write_text(
        "## Phase 0\n- [x] 끝난 일\n- [ ] 남은 일 하나\n- [ ] 남은 일 둘\n", encoding="utf-8"
    )
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert "일시정지: 아니오" in r.stdout
    assert "- [ ] 남은 일 하나" in r.stdout and "끝난 일" not in r.stdout


def test_hook_without_status_file_explains(project):
    r = _run(project)
    assert r.returncode == 0 and "uv run trader status" in r.stdout


def test_hook_truncates_at_10000(project):
    (project / "state" / "STATUS.md").write_text("가" * 12000, encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0 and len(r.stdout) <= 10_001


def test_settings_json_has_hook_and_denies_secrets():
    s = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = s["hooks"]["SessionStart"][0]
    assert "clear" in hooks["matcher"] and "session_start.py" in hooks["hooks"][0]["command"]
    deny = s["permissions"]["deny"]
    assert any("secrets/.env" in d for d in deny)
    assert any("kis order" in d for d in deny)
