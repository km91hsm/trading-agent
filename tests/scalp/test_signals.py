"""분 단위 단타 신호(Phase 23 S): 진입·청산·시간 규칙·백테스트 — 순수 함수."""

from datetime import date
from decimal import Decimal

from trader.broker.models import Candle
from trader.config import ScalpCfg
from trader.scalp import signals

CFG = ScalpCfg(enabled=True)
D = date(2026, 10, 24)


def _bar(minute_from_open: int, close, *, vol=100, high=None, low=None):
    h, m = divmod(9 * 60 + minute_from_open, 60)
    c = Decimal(str(close))
    return Candle(
        date=D,
        open=c,
        high=Decimal(str(high)) if high is not None else c,
        low=Decimal(str(low)) if low is not None else c,
        close=c,
        volume=vol,
        time=f"{h:02d}{m:02d}00",
    )


def _flat(n, price=10000, vol=100, start=0):
    return [_bar(start + i, price, vol=vol) for i in range(n)]


def test_entry_requires_volume_burst_and_double_breakout():
    base = _flat(25)  # 09:00~09:24, 25봉
    # 거래량 3배 + 종가가 당일 고가·VWAP 위 → 진입
    burst = base[:-5] + [_bar(20 + i, 10000 + 10 * i, vol=300) for i in range(5)]
    burst[-1] = _bar(24, 10100, vol=300)
    e = signals.entry_signal(burst, CFG, open_hm="09:00", close_hm="15:30")
    assert e is not None and e.price == Decimal("10100") and e.volume_ratio >= 3
    assert "거래량" in e.reason() and "09:24" in e.reason()
    # 거래량만 터지고 고가 돌파 없음(이전 봉이 더 높음) → 없음
    no_break = burst[:-1] + [_bar(24, 10030, vol=300)]
    assert signals.entry_signal(no_break, CFG, open_hm="09:00", close_hm="15:30") is None
    # 돌파는 했지만 거래량 평범 → 없음
    quiet = base[:-1] + [_bar(24, 10100, vol=120)]
    assert signals.entry_signal(quiet, CFG, open_hm="09:00", close_hm="15:30") is None
    # 봉 25개 미만 → 없음
    assert signals.entry_signal(burst[:20], CFG, open_hm="09:00", close_hm="15:30") is None


def test_entry_time_windows_warmup_and_close():
    bars = _flat(25)
    bars[-1] = _bar(24, 10100, vol=400)
    for i in range(20, 24):
        bars[i] = _bar(i, 10000, vol=400)
    # 09:24 는 warmup 15분 뒤라 가능; warmup 30분이면 금지
    assert signals.entry_signal(bars, CFG, open_hm="09:00", close_hm="15:30") is not None
    late_cfg = ScalpCfg(enabled=True, warmup_min=30)
    assert signals.entry_signal(bars, late_cfg, open_hm="09:00", close_hm="15:30") is None
    # 마감 20분 전(15:10~)은 신규 금지
    late = [_bar(360 + i, 10000, vol=100) for i in range(20)] + [
        _bar(380 + i, 10000 + 10 * i, vol=400) for i in range(5)
    ]
    assert late[-1].time == "152400"
    assert signals.entry_signal(late, CFG, open_hm="09:00", close_hm="15:30") is None


def test_exit_reasons():
    kw = dict(entry_price=Decimal("10000"), entry_time="093000", cfg=CFG, close_hm="15:30")
    assert signals.exit_reason(price=Decimal("10150"), now_time="093500", **kw) == "take_profit"
    assert signals.exit_reason(price=Decimal("9900"), now_time="093500", **kw) == "stop"
    assert signals.exit_reason(price=Decimal("10050"), now_time="103000", **kw) == "max_hold"
    assert signals.exit_reason(price=Decimal("10050"), now_time="151000", **kw) == "close"
    assert signals.exit_reason(price=Decimal("10050"), now_time="100000", **kw) is None


def test_backtest_day_and_summary():
    bars = _flat(25)
    for i in range(20, 24):
        bars[i] = _bar(i, 10000, vol=400)
    bars[-1] = _bar(24, 10100, vol=400)  # 진입 봉 09:24 @10100
    bars += [_bar(25 + i, 10100 + 20 * i, vol=100) for i in range(1, 15)]  # 상승 → 익절
    t = signals.backtest_day(bars, CFG, open_hm="09:00", close_hm="15:30")
    assert t is not None and t["entry_price"] == "10100" and t["reason"] == "take_profit"
    assert t["ret"] >= 0.015
    # 신호 없는 날은 None, 요약은 비용 차감 승률
    assert signals.backtest_day(_flat(60), CFG, open_hm="09:00", close_hm="15:30") is None
    s = signals.summarize([t, {"ret": -0.01, "reason": "stop"}], round_trip_bp=30)
    assert (
        s["trades"] == 2
        and s["win_rate"] == 0.5
        and s["by_reason"] == {"stop": 1, "take_profit": 1}
    )
    assert signals.summarize([])["trades"] == 0


def test_hedge_proposal_has_scalp_ok_default_true():
    from trader.agents import schemas

    p = schemas.HedgeProposal(
        market="KR",
        stance="NEUTRAL",
        hedge_weight=0.1,
        confidence=0.5,
        horizon_days=20,
        rationale="r",
        invalidation_conditions=[],
    )
    assert p.scalp_ok is True
    assert "scalp_ok" in schemas.HedgeProposal.model_json_schema()["properties"]
