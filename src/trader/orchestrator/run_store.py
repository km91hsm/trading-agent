"""실행 체크포인트 저장소: state/runs/<날짜>/<시장>/<종목>/<이름>.json.

Phase 3 이후 모든 단계 파일은 이 세 함수로만 읽고 쓴다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from trader.config import Market
from trader.platform import paths


def run_dir(d: date, market: Market, ticker: str, state: Path | None = None) -> Path:
    """종목별 실행 디렉터리를 만들고 반환한다."""
    base = state if state is not None else paths.state_dir()
    p = base / "runs" / d.isoformat() / market / ticker
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_checkpoint(dir: Path, name: str, data: dict) -> Path:
    """체크포인트를 pid 접미사 임시파일에 쓴 뒤 원자적으로 교체한다."""
    final = dir / f"{name}.json"
    tmp = dir / f"{name}.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, final)
    return final


def read_checkpoint(dir: Path, name: str) -> dict | None:
    """체크포인트를 읽는다. 없으면 None."""
    path = dir / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_checkpoints(dir: Path) -> list[str]:
    """존재하는 체크포인트 이름을 정렬해 돌려준다."""
    return sorted(p.stem for p in dir.glob("*.json"))
