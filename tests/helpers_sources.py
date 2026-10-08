"""수집기 테스트 공용 헬퍼 (픽스처 로드, 가짜 클라이언트)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "sources" / "fixtures"
T0 = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)  # 2026-10-09 15:00 KST


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def corpcode_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", load_bytes("dart_corpcode.xml"))
    return buf.getvalue()
