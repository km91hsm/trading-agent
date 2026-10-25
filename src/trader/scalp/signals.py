"""1분봉 단타 신호 (Phase 23 S) — 순수 함수. I/O·주문 없음.

진입: 개장 뒤 `warmup_min` 경과 ∧ 마감 `close_before_min` 전 ∧ 최근 5분 거래량 ≥ 직전 20분 평균 ×
`volume_mult` ∧ 마지막 종가가 **당일 고가(직전 봉까지)와 VWAP 을 동시에** 상향 돌파.
청산: 익절 `take_profit_pct` / 손절 `stop_pct` / 보유 `max_hold_min` / 마감 전 전량.
`backtest` 는 하루 분봉으로 같은 규칙을 재현한다(종목당 하루 1회 진입)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from statistics import mean
from typing import Any

from trader.broker.models import Candle
from trader.config import ScalpCfg

RECENT_MIN = 5
BASE_MIN = 20


def hm_minutes(hm: str) -> int:
    """ "HH:MM"·"HHMM"·"HHMMSS" → 자정 기준 분."""
    digits = hm.replace(":", "")
    return int(digits[:2]) * 60 + int(digits[2:4])


def vwap(candles: list[Candle]) -> Decimal | None:
    vol = sum(c.volume for c in candles)
    if vol <= 0:
        return None
    total = sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles)
    return (total / vol).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Entry:
    price: Decimal
    time: str  # HHMMSS
    vwap: Decimal
    day_high: Decimal
    volume_ratio: float

    def reason(self) -> str:
        return (
            f"거래량 {self.volume_ratio:.1f}배 ∧ 당일 고가 {self.day_high}·VWAP {self.vwap} "
            f"동시 돌파 @ {self.time[:2]}:{self.time[2:4]}"
        )


def entry_signal(
    candles: list[Candle], cfg: ScalpCfg, *, open_hm: str, close_hm: str
) -> Entry | None:
    """시간 오름차순 당일 1분봉으로 진입 여부. 봉이 25개 미만이면 None."""
    if len(candles) < RECENT_MIN + BASE_MIN:
        return None
    last = candles[-1]
    if not last.time:
        return None
    t = hm_minutes(last.time)
    if t < hm_minutes(open_hm) + cfg.warmup_min:
        return None
    if t >= hm_minutes(close_hm) - cfg.close_before_min:
        return None
    recent = candles[-RECENT_MIN:]
    base = candles[-(RECENT_MIN + BASE_MIN) : -RECENT_MIN]
    base_avg = mean(c.volume for c in base)
    if base_avg <= 0:
        return None
    ratio = sum(c.volume for c in recent) / (base_avg * RECENT_MIN)
    if ratio < cfg.volume_mult:
        return None
    prior = candles[:-1]
    day_high = max(c.high for c in prior)
    vw = vwap(candles)
    if vw is None or last.close <= day_high or last.close <= vw:
        return None
    return Entry(price=last.close, time=last.time, vwap=vw, day_high=day_high, volume_ratio=ratio)


def exit_reason(
    *,
    entry_price: Decimal,
    entry_time: str,
    price: Decimal,
    now_time: str,
    cfg: ScalpCfg,
    close_hm: str,
) -> str | None:
    """청산 사유(take_profit|stop|max_hold|close) 또는 None. 시각은 HHMM(SS)."""
    if entry_price <= 0:
        return None
    ret = price / entry_price - 1
    if ret >= Decimal(str(cfg.take_profit_pct)):
        return "take_profit"
    if ret <= -Decimal(str(cfg.stop_pct)):
        return "stop"
    now_m, ent_m = hm_minutes(now_time), hm_minutes(entry_time)
    if now_m >= hm_minutes(close_hm) - cfg.close_before_min:
        return "close"
    if now_m - ent_m >= cfg.max_hold_min:
        return "max_hold"
    return None


def backtest_day(
    candles: list[Candle], cfg: ScalpCfg, *, open_hm: str, close_hm: str
) -> dict[str, Any] | None:
    """하루 분봉을 봉 단위로 재생해 (최대 1회) 진입·청산을 재현. 거래가 없으면 None.
    진입은 신호 봉 종가, 청산은 사유가 난 봉 종가(손절·익절은 그 봉 종가 — 보수적 근사)."""
    ordered = sorted((c for c in candles if c.time), key=lambda c: c.time)
    for i in range(RECENT_MIN + BASE_MIN, len(ordered) + 1):
        e = entry_signal(ordered[:i], cfg, open_hm=open_hm, close_hm=close_hm)
        if e is None:
            continue
        for c in ordered[i:]:
            why = exit_reason(
                entry_price=e.price,
                entry_time=e.time,
                price=c.close,
                now_time=c.time or "",
                cfg=cfg,
                close_hm=close_hm,
            )
            if why:
                return _trade(e, c.close, c.time or "", why)
        last = ordered[-1]
        return _trade(e, last.close, last.time or "", "eod")
    return None


def _trade(e: Entry, exit_price: Decimal, exit_time: str, why: str) -> dict[str, Any]:
    ret = float(exit_price / e.price - 1)
    return {
        "entry_time": e.time,
        "entry_price": str(e.price),
        "exit_time": exit_time,
        "exit_price": str(exit_price),
        "ret": round(ret, 5),
        "reason": why,
        "volume_ratio": round(e.volume_ratio, 2),
    }


def summarize(trades: list[dict[str, Any]], *, round_trip_bp: float = 30.0) -> dict[str, Any]:
    """거래 목록 → 승률·평균·비용 차감 합계. round_trip_bp 는 왕복 비용(bp)."""
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_ret": None, "net_total": 0.0}
    rets = [t["ret"] for t in trades]
    cost = round_trip_bp / 10_000
    wins = sum(1 for r in rets if r - cost > 0)
    return {
        "trades": len(trades),
        "win_rate": round(wins / len(trades), 3),
        "avg_ret": round(sum(rets) / len(rets), 5),
        "net_total": round(sum(r - cost for r in rets), 5),
        "by_reason": {
            k: sum(1 for t in trades if t["reason"] == k)
            for k in sorted({t["reason"] for t in trades})
        },
    }
