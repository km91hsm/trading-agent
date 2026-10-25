"""시장 국면 판정 (Phase 17 H, 순수 함수 + control 플래그 저장).

3지표: ① 지수 proxy 종가 < N일(200) 단순이동평균, ② 20거래일 수익률 ≤ momentum_min(−5%),
③ 변동성 — US 는 FRED VIX ≥ vix_min(25), KR 은 proxy 20일 실현변동성(연율) ≥ realized_vol_min(25%).
count ≥ bear_min_signals → bear, 0 → bull, 그 외 neutral. bear 는 count ≤ 1 이 exit_streak_days
연속일 때만 풀린다(히스테리시스). 상태는 `control.regime:<market>` 한 곳이고 헤지·게이트·웹·STATUS
가 읽는다.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from trader.config import HedgeCfg
from trader.control import control

State = Literal["bull", "neutral", "bear"]
FLAG_PREFIX = "regime:"
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class RegimeSignals:
    close: float | None
    ma: float | None  # N일 SMA (봉이 부족하면 None → 지표 ① False)
    momentum: float | None  # momentum_days 수익률
    realized_vol: float | None  # 20일 실현변동성 연율
    vix: float | None
    ma_below: bool
    momentum_weak: bool
    vol_high: bool
    agent_risk_off: bool = False  # Phase 19: 전략가(시장 세션) RISK_OFF = 4번째 지표

    @property
    def indicator_count(self) -> int:
        """정량 지표 3개 중 켜진 수(전략가 제외)."""
        return int(self.ma_below) + int(self.momentum_weak) + int(self.vol_high)

    @property
    def count(self) -> int:
        """지표 3 + 전략가. 정량 지표가 0개면 전략가만으로 bear 가 되지 않게 raw_state 가 막는다."""
        return self.indicator_count + int(self.agent_risk_off)


def _f(v: Decimal | float | int) -> float:
    return float(v)


def classify(
    closes: list[Decimal | float],
    cfg: HedgeCfg,
    *,
    vix: float | None = None,
    agent_risk_off: bool = False,
) -> RegimeSignals:
    """종가는 날짜 오름차순(마지막이 최신). 봉이 부족한 지표는 False 로 둔다(보수적)."""
    xs = [_f(c) for c in closes if c is not None]
    close = xs[-1] if xs else None
    ma = sum(xs[-cfg.ma_days :]) / cfg.ma_days if len(xs) >= cfg.ma_days else None
    mom = None
    if len(xs) > cfg.momentum_days and xs[-1 - cfg.momentum_days] > 0:
        mom = xs[-1] / xs[-1 - cfg.momentum_days] - 1
    vol = None
    if len(xs) > cfg.momentum_days:
        tail = xs[-1 - cfg.momentum_days :]
        rets = [math.log(b / a) for a, b in zip(tail, tail[1:], strict=False) if a > 0 and b > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol = math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)
    vol_high = (vix is not None and vix >= cfg.vix_min) or (
        vix is None and vol is not None and vol >= cfg.realized_vol_min
    )
    return RegimeSignals(
        close=close,
        ma=ma,
        momentum=mom,
        realized_vol=vol,
        vix=vix,
        ma_below=bool(close is not None and ma is not None and close < ma),
        momentum_weak=bool(mom is not None and mom <= cfg.momentum_min),
        vol_high=bool(vol_high),
        agent_risk_off=bool(agent_risk_off),
    )


def raw_state(signals: RegimeSignals, cfg: HedgeCfg) -> State:
    """count ≥ bear_min_signals → bear, 0 → bull, 그 외 neutral. 정량 지표가 하나도 없으면 전략가
    RISK_OFF 만으로는 neutral 까지(bear 진입은 지표 1개 이상 + 전략가)."""
    if signals.indicator_count == 0:
        return "neutral" if signals.agent_risk_off else "bull"
    if signals.count >= cfg.bear_min_signals:
        return "bear"
    return "bull" if signals.count == 0 else "neutral"


def next_state(
    prev: State | None, prev_streak: int, signals: RegimeSignals, cfg: HedgeCfg
) -> tuple[State, int]:
    """히스테리시스: bear 에서는 count ≤ 1 이 exit_streak_days 연속이어야 벗어난다.
    streak 은 bear 중 '해제 조건 충족 연속 일수'."""
    raw = raw_state(signals, cfg)
    if prev == "bear" and raw != "bear":
        streak = prev_streak + 1
        if streak >= cfg.exit_streak_days:
            return raw, 0
        return "bear", streak
    return raw, 0


def load(conn: sqlite3.Connection, market: str) -> dict | None:
    return control.get_flag(conn, f"{FLAG_PREFIX}{market}")


def update(
    conn: sqlite3.Connection,
    market: str,
    *,
    closes: list[Decimal | float],
    cfg: HedgeCfg,
    today: date,
    vix: float | None = None,
    now: datetime | None = None,
    agent_risk_off: bool = False,
) -> dict:
    """하루 1회: 지표 계산 → 히스테리시스 → 플래그 저장. 같은 날 재호출은 streak 을 겹치지 않음."""
    sig = classify(closes, cfg, vix=vix, agent_risk_off=agent_risk_off)
    return _apply(conn, market, sig, cfg=cfg, today=today, now=now)


def reapply(
    conn: sqlite3.Connection,
    market: str,
    *,
    agent_risk_off: bool,
    cfg: HedgeCfg,
    today: date,
    now: datetime | None = None,
) -> dict | None:
    """저장된 오늘 지표에 전략가 신호만 바꿔 다시 판정한다(네트워크 없음). 오늘 판정이 없으면 None.
    데몬이 시장 세션 뒤에 부른다 — 같은 운영일 재판정이라 streak 이 겹치지 않는다."""
    prev = load(conn, market) or {}
    if prev.get("asof") != today.isoformat() or not prev.get("signals"):
        return None
    raw = {k: v for k, v in (prev.get("signals") or {}).items() if k != "agent_risk_off"}
    sig = RegimeSignals(**raw, agent_risk_off=agent_risk_off)
    return _apply(conn, market, sig, cfg=cfg, today=today, now=now)


def _apply(
    conn: sqlite3.Connection,
    market: str,
    sig: RegimeSignals,
    *,
    cfg: HedgeCfg,
    today: date,
    now: datetime | None,
) -> dict:
    prev = load(conn, market) or {}
    if prev.get("asof") == today.isoformat() and prev.get("state"):
        # 같은 운영일 재계산: 어제 상태·streak 에서 다시 판정 (중복 누적 방지)
        base_state, base_streak = prev.get("prev_state"), int(prev.get("prev_streak") or 0)
    else:
        base_state, base_streak = prev.get("state"), int(prev.get("streak") or 0)
    state, streak = next_state(base_state, base_streak, sig, cfg)
    since = prev.get("since") if prev.get("state") == state else today.isoformat()
    out = {
        "market": market,
        "state": state,
        "count": sig.count,
        "signals": asdict(sig),
        "streak": streak,
        "since": since,
        "asof": today.isoformat(),
        "updated_at": (now or datetime.now()).isoformat(),
        "prev_state": base_state,
        "prev_streak": base_streak,
    }
    control.set_flag(conn, f"{FLAG_PREFIX}{market}", out)
    return out


def backtest(
    rows: list[tuple[date, Decimal | float]], cfg: HedgeCfg, *, vix_by_date: dict | None = None
) -> list[dict]:
    """과거 종가열로 날짜별 상태를 재현해 bear 구간 목록을 돌려준다 (CLI `regime backtest`)."""
    closes: list[float] = []
    state: State | None = None
    streak = 0
    spans: list[dict] = []
    cur: dict | None = None
    for d, c in rows:
        closes.append(_f(c))
        sig = classify(closes, cfg, vix=(vix_by_date or {}).get(d))
        state, streak = next_state(state, streak, sig, cfg)
        if state == "bear" and cur is None:
            cur = {"start": d.isoformat(), "end": d.isoformat(), "max_count": sig.count}
        elif state == "bear" and cur is not None:
            cur["end"] = d.isoformat()
            cur["max_count"] = max(cur["max_count"], sig.count)
        elif state != "bear" and cur is not None:
            spans.append(cur)
            cur = None
    if cur is not None:
        cur["open"] = True
        spans.append(cur)
    return spans


def _view_today(conn: sqlite3.Connection, market: str, today: date | None) -> dict | None:
    """오늘 시장 세션 제안(Phase 18) 요약. today 가 없으면 KST 오늘."""
    from trader.config import KST
    from trader.orchestrator.tools import market_view_today

    d = today or datetime.now(KST).date()
    v = market_view_today(conn, market, d)
    if not v:
        return None
    return {k: v.get(k) for k in ("stance", "hedge_weight", "leveraged", "confidence")}


def _scorecard(conn: sqlite3.Connection, market: str) -> dict | None:
    from trader.orchestrator.market_view import scorecard

    try:
        sc = scorecard(conn, market)
    except sqlite3.Error:
        return None
    return sc if sc["n"] else None


def summary(
    conn: sqlite3.Connection, markets: list[str], *, today: date | None = None
) -> list[dict]:
    """시장별 국면 요약 (웹 /api/regime·STATUS·텔레그램). 기록이 없으면 state None.
    `view` 는 오늘 시장 세션의 제안(없으면 None), `scorecard` 는 제안 적중률(기록 없으면 None)."""
    out = []
    for m in markets:
        r = load(conn, m) or {}
        sig = r.get("signals") or {}
        out.append(
            {
                "view": _view_today(conn, m, today),
                "scorecard": _scorecard(conn, m),
                "market": m,
                "state": r.get("state"),
                "count": r.get("count"),
                "since": r.get("since"),
                "asof": r.get("asof"),
                "signals": {
                    k: sig.get(k)
                    for k in ("ma_below", "momentum_weak", "vol_high", "agent_risk_off")
                },
                "values": {
                    k: sig.get(k) for k in ("close", "ma", "momentum", "realized_vol", "vix")
                },
            }
        )
    return out


def line(conn: sqlite3.Connection, markets: list[str], *, today: date | None = None) -> str:
    """한 줄 표기: 'KR bear(2/3) 10-24~ 제안 12% · US bull(0/3)'. 기록 없으면 '-'."""
    parts = []
    for r in summary(conn, markets, today=today):
        if r["state"] is None:
            parts.append(f"{r['market']} -")
        else:
            since = f" {r['since'][5:]}~" if r.get("since") else ""
            v = r.get("view")
            prop = f" 제안 {float(v['hedge_weight'] or 0):.0%}" if v else ""
            parts.append(f"{r['market']} {r['state']}({count_text(r)}){since}{prop}")
    return " · ".join(parts)


def count_text(r: dict) -> str:
    """'2/3' 또는 전략가 지표가 켜졌으면 '1/3+전략가'."""
    sig = r.get("signals") or {}
    n = sum(int(bool(sig.get(k))) for k in ("ma_below", "momentum_weak", "vol_high"))
    if r.get("count") is None:
        return "-/3"
    return f"{n}/3+전략가" if sig.get("agent_risk_off") else f"{n}/3"
