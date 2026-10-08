"""SessionStart 훅: state/STATUS.md 와 docs/TODO.md 미완료 항목을 컨텍스트로 출력한다.

표준 라이브러리만 사용하고 macOS·Windows 공용이다. 출력은 UTF-8, 전체 10,000자 상한.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MAX_CHARS = 10_000
MAX_TODO_ITEMS = 30


def _root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _open_todos(text: str, limit: int) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            items.append(stripped)
            if len(items) >= limit:
                break
    return items


def main() -> int:
    root = _root()
    parts: list[str] = []
    status = _read(root / "state" / "STATUS.md")
    if status.strip():
        parts.append(status.rstrip())
    else:
        parts.append(
            "# 시스템 상태\n\nstate/STATUS.md 가 아직 없습니다. "
            "`uv run trader status` 로 생성하세요 (Phase 0 완료 전이면 정상)."
        )
    todos = _open_todos(_read(root / "docs" / "TODO.md"), MAX_TODO_ITEMS)
    if todos:
        parts.append(f"## 미완료 TODO (상위 {len(todos)})\n" + "\n".join(todos))
    text = "\n\n".join(parts)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 20].rstrip() + "\n\n…(10,000자 초과로 잘림)"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
