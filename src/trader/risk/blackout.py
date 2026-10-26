"""권리 일정 블랙아웃(Stock D-108·D-113 교훈): 유상증자(희석)·무상증자·액면교체(매매정지)·합병분할은
기준일 앞뒤로 가격이 크게 움직이거나 거래가 멈춘다. 예탁원 확정 기준일을 매일 모아 두고(`collect`
→ `control.ksd_events:KR`), `event_blackout.enabled` 면 기준일 `before_days` 전 ~ `after_days` 뒤의
신규 매수를 거부한다(`executor.execute` 가 trade_block 에 합친다 — 매도 무관). **기본 끔**: 일정
이력이 짧아 효과를 아직 잴 수 없다. 국내만."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from trader.config import BlackoutCfg
from trader.control import control

logger = logging.getLogger("trader.risk")

FLAG = "ksd_events:KR"
AHEAD_DAYS = 30
LABEL = {
    "paidin": "유상증자",
    "bonus": "무상증자",
    "rev_split": "액면교체",
    "merger_split": "합병·분할",
}


def collect(market_client: Any, cfg: BlackoutCfg, *, today: date) -> dict:
    """종류별 [today − after_days, today + AHEAD_DAYS] 기준일 일정. 종류 하나의 실패는 건너뛴다."""
    events: list[dict] = []
    errors: dict[str, str] = {}
    lo, hi = today - timedelta(days=cfg.after_days), today + timedelta(days=AHEAD_DAYS)
    for kind in cfg.kinds:
        try:
            events += market_client.ksd_schedule(kind, lo, hi)
        except Exception as e:  # noqa: BLE001 - 미확인 TR: 실패해도 다른 종류·매매는 간다
            errors[kind] = f"{type(e).__name__}: {e}"
            logger.warning("예탁원 일정 수집 실패 %s: %s", kind, e)
    return {"at": datetime.now(UTC).isoformat(), "events": events, "errors": errors}


def store(conn: sqlite3.Connection, result: dict) -> None:
    control.set_flag(conn, FLAG, result)


def reason(conn: sqlite3.Connection, ticker: str, today: date, cfg: BlackoutCfg) -> str | None:
    """오늘이 그 종목 기준일의 블랙아웃 창 안이면 사유, 아니면 None(꺼져 있어도 None)."""
    if not cfg.enabled:
        return None
    data = control.get_flag(conn, FLAG) or {}
    for ev in data.get("events") or []:
        if ev.get("ticker") != ticker or ev.get("kind") not in cfg.kinds:
            continue
        try:
            d = date.fromisoformat(str(ev.get("date")))
        except ValueError:
            continue
        if d - timedelta(days=cfg.before_days) <= today <= d + timedelta(days=cfg.after_days):
            return f"권리 일정: {LABEL.get(ev['kind'], ev['kind'])} 기준일 {d.isoformat()}"
    return None
