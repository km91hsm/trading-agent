"""원장 백업(Stock D-075 교훈): 주문·승인·실행 키·결정은 다시 만들 수 없다 — 매일 한 번
SQLite 백업 API 로 일관된 사본을 `state/backups/trader-<UTC 시각>.db` 에 남기고, 최근 일별
`keep_daily` 개 + 그보다 오래된 월요일 사본 `keep_weekly` 개만 둔다. 데몬 잡 `ledger_backup`.
복원은 데몬을 끈 뒤 사본을 `state/trader.db` 로 복사(WAL 파일 `-wal`·`-shm` 은 지운다)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trader.platform import paths

logger = logging.getLogger("trader.ledger")

KEEP_DAILY = 14
KEEP_WEEKLY = 8
_PREFIX = "trader-"


def backup_dir(state: Path | None = None) -> Path:
    return (state or paths.state_dir()) / "backups"


def backup(*, state: Path | None = None, now: datetime | None = None) -> Path:
    """원장 전체를 백업 API 로 복사한다(쓰는 중이어도 일관된 시점). 임시 파일에 쓰고 무결성
    확인 뒤 이름을 바꾼다 — 실패한 사본이 정상 백업처럼 남지 않게."""
    base = state or paths.state_dir()
    out_dir = backup_dir(base)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d_%H%M%S")
    final = out_dir / f"{_PREFIX}{stamp}.db"
    tmp = final.with_suffix(".db.tmp")
    src = sqlite3.connect(base / "trader.db", timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"백업 무결성 확인 실패: {ok}")
    tmp.replace(final)
    logger.info("원장 백업 %s", final.name)
    return final


def _day(p: Path) -> date | None:
    try:
        return datetime.strptime(p.name[len(_PREFIX) : len(_PREFIX) + 8], "%Y%m%d").date()
    except ValueError:
        return None


def prune(
    folder: Path, *, today: date, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY
) -> int:
    """보존 규칙 밖의 백업을 지운다(멱등). 지운 개수."""
    recent_from = today - timedelta(days=keep_daily - 1)
    files = [(d, p) for p in folder.glob(f"{_PREFIX}*.db") if (d := _day(p)) is not None]
    weekly = sorted({d for d, _ in files if d < recent_from and d.weekday() == 0}, reverse=True)[
        :keep_weekly
    ]
    removed = 0
    for d, p in files:
        if d >= recent_from or d in weekly:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            logger.warning("백업 정리 실패 %s: %s", p.name, e)
    return removed
