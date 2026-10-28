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
STALE_DAYS = 3  # 종류별 마지막 성공 수집이 이보다 오래되면 '오래됨'(알림, 2-O9)
LABEL = {
    "paidin": "유상증자",
    "bonus": "무상증자",
    "rev_split": "액면교체",
    "merger_split": "합병·분할",
}


def collect(
    market_client: Any, cfg: BlackoutCfg, *, today: date, now: datetime | None = None
) -> dict:
    """종류별 [today − after_days, today + AHEAD_DAYS] 기준일 일정. 종류 하나의 실패는 건너뛴다
    (`errors[kind]`). `kinds` 는 시도한 종류 — `store` 가 종류별로 합칠 때 쓴다."""
    events: list[dict] = []
    errors: dict[str, str] = {}
    lo, hi = today - timedelta(days=cfg.after_days), today + timedelta(days=AHEAD_DAYS)
    for kind in cfg.kinds:
        try:
            events += market_client.ksd_schedule(kind, lo, hi)
        except Exception as e:  # noqa: BLE001 - 미확인 TR: 실패해도 다른 종류·매매는 간다
            errors[kind] = f"{type(e).__name__}: {e}"
            logger.warning("예탁원 일정 수집 실패 %s: %s", kind, e)
    return {
        "at": (now or datetime.now(UTC)).isoformat(),
        "kinds": list(cfg.kinds),
        "events": events,
        "errors": errors,
    }


def store(conn: sqlite3.Connection, result: dict) -> dict:
    """종류별 마지막 성공분을 합쳐 저장한다(2-O9): 이번에 실패한 종류는 지난 일정·수집 시각을
    유지하고 성공한 종류만 교체, 설정에서 빠진 종류는 버린다 — 한 종류의 TR 실패가 빈 일정으로
    블랙아웃을 조용히 끄지 않게. `fetched_at[kind]` 가 종류별 마지막 성공 시각."""
    prev = control.get_flag(conn, FLAG) or {}
    kinds = list(result.get("kinds") or [])
    failed = {k for k in (result.get("errors") or {}) if k in kinds}
    events = [e for e in result.get("events") or [] if e.get("kind") not in failed]
    events += [e for e in prev.get("events") or [] if e.get("kind") in failed]
    prev_at = prev.get("fetched_at") or {}
    fetched = {k: (prev_at.get(k) if k in failed else result.get("at")) for k in kinds}
    data = {
        "at": result.get("at"),
        "kinds": kinds,
        "events": events,
        "errors": dict(result.get("errors") or {}),
        "fetched_at": {k: v for k, v in fetched.items() if v},
    }
    control.set_flag(conn, FLAG, data)
    return data


def stale_kinds(data: dict | None, cfg: BlackoutCfg, *, now: datetime) -> dict[str, int | None]:
    """설정 종류 중 마지막 성공 수집이 STALE_DAYS 넘게 지난 것 → {종류: 지난 일수}. 수집 기록이
    없는 종류는 None."""
    out: dict[str, int | None] = {}
    fetched = (data or {}).get("fetched_at") or {}
    for kind in cfg.kinds:
        iso = fetched.get(kind)
        if not iso:
            out[kind] = None
            continue
        try:
            days = (now - datetime.fromisoformat(str(iso))).days
        except (TypeError, ValueError):
            out[kind] = None
            continue
        if days > STALE_DAYS:
            out[kind] = days
    return out


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
