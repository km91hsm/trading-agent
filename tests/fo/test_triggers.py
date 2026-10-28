"""파생 재평가 트리거(Phase 26 Task 4.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fo._gate import cfg, position
from trader.config import KST, HorizonProfile
from trader.fo import triggers
from trader.ledger import repo

DAY = date(2026, 10, 27)
PROFILE = HorizonProfile(move_pct=0.04, reeval_interval_min=15, periodic_check_min=60,
                         news_min_novel=1, daily_reanalyze_every=1)  # fmt: skip
LONG_PROFILE = HorizonProfile(move_pct=0.10, reeval_interval_min=60, periodic_check_min=0,
                              news_min_novel=3, daily_reanalyze_every=5)  # fmt: skip


def at(h, m=0, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=KST).astimezone(UTC)


def _check(conn, *, now, pos=None, c=None, **kw):
    kw.setdefault("underlying", Decimal("350"))
    kw.setdefault("iv", None)
    kw.setdefault("ref_index", Decimal("350"))
    return triggers.check(conn, position=pos or position(), cfg=c or cfg(), profile=PROFILE,
                          now=now, today=DAY, **kw)  # fmt: skip


def _fire(conn, trig, *, now, handled=True, session="sess-1", pos=None):
    tid = triggers.record(conn, pos or position(), trig, now=now)
    if handled:
        repo.mark_trigger_handled(conn, tid, session, handled_at=now.isoformat())
    return tid


def test_once_per_day_kinds(conn):
    now = at(10, 0)
    t = _check(conn, now=now, underlying=Decimal("365"))  # +4.3% ≥ 4%
    assert t.kind == "move"
    _fire(conn, t, now=now)
    later = at(11, 0)
    again = _check(conn, now=later, underlying=Decimal("366"))
    assert again is None or again.kind != "move"


def test_periodic_bucket_and_daily_cap(conn):
    c = cfg(reeval={"periodic_min": 60, "periodic_max_per_day": 2, "interval_min": 30})
    t = _check(conn, now=at(9, 50), c=c)
    assert t.kind == "periodic"
    _fire(conn, t, now=at(9, 50))
    assert _check(conn, now=at(10, 30), c=c) is None  # 같은 버킷(09:45~10:45)
    t2 = _check(conn, now=at(10, 50), c=c)
    assert t2.kind == "periodic"
    _fire(conn, t2, now=at(10, 50))
    assert _check(conn, now=at(12, 0), c=c) is None  # 하루 2회 상한


def test_priority_bundles_one(conn):
    t = _check(conn, now=at(10, 0), underlying=Decimal("365"), novel_news=2,
               invalidation_conditions=["지수 360 이상이면 무효"])  # fmt: skip
    assert t.kind == "invalidation"
    assert t.detail["bundled"][:2] == ["move", "news"]


def test_pending_suppresses_new(conn):
    t = _check(conn, now=at(10, 0), underlying=Decimal("365"))
    _fire(conn, t, now=at(10, 0), handled=False)
    assert _check(conn, now=at(11, 0), novel_news=5) is None


def test_failed_session_waits_interval_then_retries(conn):
    t = _check(conn, now=at(10, 0), underlying=Decimal("365"))
    _fire(conn, t, now=at(10, 0), session="실패: 상한")
    # 실패도 간격은 지킨다(실패한 유료 세션이 매 틱 다시 뜨지 않게, 단계 5 리뷰)
    assert _check(conn, now=at(10, 5), underlying=Decimal("365")) is None
    again = _check(conn, now=at(10, 31), underlying=Decimal("365"))
    assert again is not None and again.kind == "move"  # 운영일 1회는 성공만 센다


def test_interval_and_daily_max(conn):
    c = cfg(reeval={"interval_min": 30, "max_per_day": 2, "periodic_max_per_day": 0})
    t = _check(conn, now=at(10, 0), c=c, novel_news=1)
    _fire(conn, t, now=at(10, 0))
    assert _check(conn, now=at(10, 20), c=c, underlying=Decimal("365")) is None  # 30분 안
    t2 = _check(conn, now=at(10, 40), c=c, underlying=Decimal("365"))
    assert t2.kind == "move"
    _fire(conn, t2, now=at(10, 40))
    assert _check(conn, now=at(13, 0), c=c, take_profit_pt=Decimal("350")) is None  # 하루 2회


def test_expiry_opinion_day_before_force_close():
    pos = position(vehicle="OPTION_SELL", kind="CALL", contract_side="SHORT", direction="SHORT")
    last = date(2026, 11, 12)  # 목요일
    force = triggers.force_close_day_of(pos, cfg(roll_days=2), last_trade_date=last)
    assert force == date(2026, 11, 9)  # D-3 운영일(월)
    assert triggers.opinion_day_of(force) == date(2026, 11, 6)  # D-4(금)
    fut = position()
    assert triggers.force_close_day_of(fut, cfg(roll_days=2), last_trade_date=last) == date(
        2026, 11, 10
    )


def test_expiry_trigger_after_0905_once(conn):
    c = cfg(reeval={"periodic_max_per_day": 0})
    assert _check(conn, now=at(9, 0), c=c, opinion_day=DAY) is None
    t = _check(conn, now=at(9, 6), c=c, opinion_day=DAY)
    assert t.kind == "expiry"
    _fire(conn, t, now=at(9, 6))
    assert _check(conn, now=at(13, 0), c=c, opinion_day=DAY) is None
    assert _check(conn, now=at(9, 6), c=c, opinion_day=DAY + timedelta(days=1)) is None


def test_news_threshold_by_profile(conn):
    c = cfg(reeval={"periodic_max_per_day": 0})
    short = triggers.check(conn, position=position(), cfg=c, profile=PROFILE, now=at(10, 0),
                           today=DAY, underlying=None, iv=None, novel_news=1)  # fmt: skip
    assert short.kind == "news"
    long_ = triggers.check(conn, position=position(), cfg=c, profile=LONG_PROFILE,
                           now=at(10, 0), today=DAY, underlying=None, iv=None,
                           novel_news=2)  # fmt: skip
    assert long_ is None


def test_take_profit_near_by_direction_and_iv_spike(conn):
    c = cfg(reeval={"periodic_max_per_day": 0, "tp_near_pct": 0.002})
    t = _check(conn, now=at(10, 0), c=c, take_profit_pt=Decimal("350.5"))  # 350 ≥ 350.5×0.998
    assert t.kind == "take_profit"
    short = position(direction="SHORT", contract_side="SHORT", id=2, code="105W12")
    assert _check(conn, now=at(10, 0), c=c, pos=short, take_profit_pt=Decimal("349")) is None
    hit = _check(conn, now=at(10, 0), c=c, pos=short, take_profit_pt=Decimal("349.5"))
    assert hit.kind == "take_profit"  # 350 ≤ 349.5×1.002
    opt = position(kind="CALL", vehicle="OPTION_BUY", id=3, code="205W11350")
    spike = _check(conn, now=at(10, 0), c=c, pos=opt, iv=26.0, ref_iv=18.0)
    assert spike.kind == "iv_spike"
