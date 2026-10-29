"""실제 적용 손절선(모니터와 화면이 같은 함수를 쓴다)."""

from decimal import Decimal

from trader.risk.stops import effective_stop


def test_decision_stop_when_no_floor():
    assert effective_stop(95, None, Decimal("100"), -0.15) == (Decimal("95"), "decision")


def test_floor_wins_when_higher_or_decision_missing():
    assert effective_stop(90, Decimal("94"), Decimal("100"), -0.15) == (Decimal("94"), "floor")
    assert effective_stop(None, Decimal("94"), Decimal("100"), -0.15) == (Decimal("94"), "floor")


def test_decision_kept_when_not_below_floor():
    assert effective_stop(96, Decimal("94"), Decimal("100"), -0.15) == (Decimal("96"), "decision")
    assert effective_stop(94, Decimal("94"), Decimal("100"), -0.15) == (Decimal("94"), "decision")


def test_default_from_avg_price():
    assert effective_stop(None, None, Decimal("100"), -0.15) == (Decimal("85.00"), "default")
    assert effective_stop(0, None, Decimal("100"), -0.15)[1] == "default"  # 0 은 없음과 같다


def test_none_without_any_basis():
    assert effective_stop(None, None, Decimal("0"), -0.15) == (None, None)
    assert effective_stop(None, None, None, -0.15) == (None, None)
