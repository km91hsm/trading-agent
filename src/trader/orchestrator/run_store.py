"""실행 체크포인트 저장소: state/runs/<날짜>/<시장>/<종목>/<이름>.json.

Phase 3 이후 모든 단계 파일은 이 세 함수로만 읽고 쓴다.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from trader.config import KST, MARKET_TZ, Market
from trader.platform import paths
from trader.platform.fsutil import atomic_replace

_US_OPEN = time(9, 30)


def run_date_for(market: str, now: datetime) -> date:
    """시장의 운영일(run 디렉터리 날짜). KR 은 KST 날짜.

    US 장은 KST 자정을 넘겨 열리므로(09:30~16:00 ET = 22:30/23:30 KST ~ 05:00/06:00 KST 다음날)
    현지 날짜의 개장 시각을 KST 로 바꾼 날짜를 쓴다. analyze(개장 전, KST 날짜 D)와 장중
    monitor·재평가(KST 자정 이후)가 같은 디렉터리·같은 주문 창을 보게 된다."""
    tz = MARKET_TZ.get(market, KST)
    local = now.astimezone(tz)
    if market == "US":
        return datetime.combine(local.date(), _US_OPEN, tzinfo=tz).astimezone(KST).date()
    return local.date()


def run_dir(d: date, market: Market, ticker: str, state: Path | None = None) -> Path:
    """종목별 실행 디렉터리를 만들고 반환한다."""
    base = state if state is not None else paths.state_dir()
    root = (base / "runs").resolve()
    p = (root / d.isoformat() / market / ticker).resolve()
    if root not in p.parents:
        raise ValueError(f"실행 디렉터리가 state/runs 밖을 가리킵니다: {market}/{ticker}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def market_dir(d: date, market: Market, state: Path | None = None, *, create: bool = False) -> Path:
    """`state/runs/<날짜>/<시장>/` (시장 단위 산출물 `_scan`·`_screen`·`_reflect` 자리)."""
    base = state if state is not None else paths.state_dir()
    p = base / "runs" / d.isoformat() / market
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def checkpoint_payload(data: Any, call: dict | None = None) -> dict:
    """체크포인트 파일 봉투 {schema_version, data, call}. tools·executor 가 같은 모양으로 쓴다."""
    from trader.agents import schemas

    return {"schema_version": schemas.CHECKPOINT_SCHEMA_VERSION, "data": data, "call": call}


def write_checkpoint(dir: Path, name: str, data: dict) -> Path:
    """체크포인트를 pid·스레드 접미사 임시파일에 쓴 뒤 원자적으로 교체한다(데몬은 monitor·작업
    대기열이 한 프로세스의 다른 스레드라 pid 만으로는 같은 단계 동시 쓰기에서 임시파일이 겹친다)."""
    final = dir / f"{name}.json"
    tmp = dir / f"{name}.json.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_replace(tmp, final)
    return final


def read_checkpoint(dir: Path, name: str) -> dict | None:
    """체크포인트를 읽는다. 없으면 None."""
    path = (dir / f"{name}.json").resolve()
    if dir.resolve() != path.parent:
        raise ValueError(f"체크포인트 이름이 디렉터리 밖을 가리킵니다: {name}")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_checkpoints(dir: Path) -> list[str]:
    """존재하는 체크포인트 이름을 정렬해 돌려준다."""
    return sorted(p.stem for p in dir.glob("*.json"))
