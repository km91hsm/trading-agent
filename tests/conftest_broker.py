"""브로커 테스트 공용 헬퍼 (픽스처 JSON 로드)."""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "broker" / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
