from datetime import date, timedelta

from trader.config import HedgeCfg
from trader.risk import regime

CFG = HedgeCfg(ma_days=20, momentum_days=5, exit_streak_days=2)


def _series(*segments: tuple[int, float]) -> list[float]:
    """(길이, 일일 변화율) 구간을 이어 붙인 종가열. 시작 100."""
    xs = [100.0]
    for n, r in segments:
        for _ in range(n):
            xs.append(xs[-1] * (1 + r))
    return xs


def test_classify_each_signal():
    flat = _series((30, 0.0))
    s = regime.classify(flat, CFG)
    assert s.count == 0 and s.ma == 100.0 and s.momentum == 0.0 and s.realized_vol == 0.0
    down = _series((30, 0.0), (6, -0.02))  # 20일선 아래 + 5일 −9.6%
    s = regime.classify(down, CFG)
    assert s.ma_below and s.momentum_weak and not s.vol_high and s.count == 2
    assert regime.raw_state(s, CFG) == "bear"
    s = regime.classify(flat, CFG, vix=30.0)  # VIX 만
    assert s.vol_high and s.count == 1 and regime.raw_state(s, CFG) == "neutral"
    # VIX 가 없으면 실현변동성: 하루 ±8% 톱니 → 연율 100% 넘음
    saw = _series(*([(1, 0.08), (1, -0.08)] * 15))
    assert regime.classify(saw, CFG).vol_high
    # 봉 부족: MA 지표는 False (보수적), 나머지는 계산
    assert regime.classify(_series((3, -0.05)), CFG).ma is None


def test_hysteresis_exit_needs_streak():
    bear = regime.classify(_series((30, 0.0), (6, -0.02)), CFG)
    calm = regime.classify(_series((30, 0.0)), CFG)
    assert regime.next_state(None, 0, bear, CFG) == ("bear", 0)
    assert regime.next_state("bear", 0, calm, CFG) == ("bear", 1)  # 1일째: 아직 bear
    assert regime.next_state("bear", 1, calm, CFG) == ("bull", 0)  # 2일 연속 → 해제
    assert regime.next_state("bear", 1, bear, CFG) == ("bear", 0)  # 다시 악화면 streak 리셋
    assert regime.next_state("neutral", 0, calm, CFG) == ("bull", 0)


def test_update_persists_flag_and_is_idempotent_within_day(conn):
    d = date(2026, 10, 24)
    out = regime.update(conn, "KR", closes=_series((30, 0.0), (6, -0.02)), cfg=CFG, today=d)
    assert out["state"] == "bear" and out["since"] == d.isoformat()
    assert regime.load(conn, "KR")["count"] == 2 and regime.load(conn, "US") is None
    calm = _series((30, 0.0))
    d2 = d + timedelta(days=1)
    o1 = regime.update(conn, "KR", closes=calm, cfg=CFG, today=d2)
    o2 = regime.update(conn, "KR", closes=calm, cfg=CFG, today=d2)  # 같은 날 재실행
    assert o1["state"] == "bear" and o1["streak"] == 1
    assert o2 == {**o1, "updated_at": o2["updated_at"]}
    o3 = regime.update(conn, "KR", closes=calm, cfg=CFG, today=d2 + timedelta(days=1))
    assert o3["state"] == "bull" and o3["since"] == (d2 + timedelta(days=1)).isoformat()


def test_backtest_spans():
    xs = _series((30, 0.0), (6, -0.02), (10, 0.0))
    rows = [(date(2026, 1, 1) + timedelta(days=i), x) for i, x in enumerate(xs)]
    spans = regime.backtest(rows, CFG)
    assert len(spans) == 1 and spans[0]["max_count"] == 2 and "open" not in spans[0]
    assert spans[0]["start"] < spans[0]["end"]


def test_agent_risk_off_is_fourth_signal_but_cannot_enter_bear_alone(conn):
    """Phase 19: 전략가 RISK_OFF 는 count 에 1 을 더한다. 정량 지표 0개면 neutral 까지, 1개 +
    전략가면 bear(bear_min_signals=2). reapply 는 저장된 지표에 전략가 신호만 바꿔 같은 날
    재판정한다."""
    flat = _series((30, 0.0))
    s = regime.classify(flat, CFG, agent_risk_off=True)
    assert s.count == 1 and s.indicator_count == 0 and regime.raw_state(s, CFG) == "neutral"
    one = regime.classify(flat, CFG, vix=30.0, agent_risk_off=True)  # VIX 1개 + 전략가
    assert one.count == 2 and regime.raw_state(one, CFG) == "bear"
    d = date(2026, 10, 24)
    out = regime.update(conn, "KR", closes=flat, cfg=CFG, today=d, vix=30.0)
    assert out["state"] == "neutral" and out["count"] == 1
    re = regime.reapply(conn, "KR", agent_risk_off=True, cfg=CFG, today=d)
    assert re["state"] == "bear" and re["count"] == 2 and re["signals"]["agent_risk_off"] is True
    assert re["prev_state"] is None  # 같은 날 재판정: 어제 상태 기준 (streak 겹침 없음)
    assert regime.count_text(re) == "1/3+전략가"
    # 신호를 끄면 되돌아온다. 오늘 판정이 없는 시장은 None
    back = regime.reapply(conn, "KR", agent_risk_off=False, cfg=CFG, today=d)
    assert back["state"] == "neutral" and regime.count_text(back) == "1/3"
    assert regime.reapply(conn, "US", agent_risk_off=True, cfg=CFG, today=d) is None
    assert (
        regime.reapply(conn, "KR", agent_risk_off=True, cfg=CFG, today=d + timedelta(days=1))
        is None
    )
    assert regime.summary(conn, ["KR"])[0]["signals"]["agent_risk_off"] is False
