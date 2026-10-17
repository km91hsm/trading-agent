"""테스트가 저장소 파일을 가리킬 때 쓰는 경로 상수."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_AGENTS = REPO_ROOT / ".claude" / "agents"
