"""사용자 수동 입력 (스펙 §3): state/manual_inputs/<ticker>.md | .json. 수정 시각이 received_at."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from trader.platform import paths

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")


def _dir(state: Path | None) -> Path:
    base = state if state is not None else paths.state_dir()
    return base / "manual_inputs"


def read(ticker: str, *, state: Path | None = None) -> dict | None:
    """없으면 None. md → {format, text}, json → {format, data}, 파싱 실패 → {error}."""
    if not _TICKER_RE.match(ticker or ""):
        return None
    d = _dir(state)
    for ext in ("md", "json"):
        p = d / f"{ticker}.{ext}"
        if not p.is_file():
            continue
        updated = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"format": ext, "path": str(p), "updated_at": updated, "error": str(e)}
        if ext == "md":
            return {"format": "md", "path": str(p), "updated_at": updated, "text": text}
        try:
            return {
                "format": "json",
                "path": str(p),
                "updated_at": updated,
                "data": json.loads(text),
            }
        except ValueError as e:
            return {
                "format": "json",
                "path": str(p),
                "updated_at": updated,
                "error": f"JSON 오류: {e}",
            }
    return None
