from decimal import Decimal

from trader.config import FoCfg
from trader.risk import ticks

MINI = FoCfg().products["mini"]
REG = FoCfg().products["regular"]


def test_mini_option_tick_boundaries():
    t = ticks.tick_table_for(MINI, "CALL")
    assert ticks.tick_for(t, Decimal("2.99")) == Decimal("0.01")
    assert ticks.tick_for(t, Decimal("3.00")) == Decimal("0.02")
    assert ticks.tick_for(t, Decimal("9.98")) == Decimal("0.02")
    assert ticks.tick_for(t, Decimal("10.00")) == Decimal("0.05")
    assert ticks.tick_for(ticks.tick_table_for(MINI, "FUTURES"), Decimal("350")) == Decimal("0.02")
    assert ticks.tick_for(ticks.tick_table_for(REG, "PUT"), Decimal("9.99")) == Decimal("0.01")
    assert ticks.tick_for(ticks.tick_table_for(REG, "FUTURES"), Decimal("1")) == Decimal("0.05")


def test_quantize_crossing_boundary():
    t = ticks.tick_table_for(MINI, "CALL")
    assert ticks.quantize_tick(t, Decimal("2.996")) == Decimal("3.00")
    assert ticks.quantize_tick(t, Decimal("3.01")) == Decimal("3.02")
    assert ticks.quantize_tick(t, Decimal("9.99")) == Decimal("10.00")
    assert ticks.quantize_tick(t, Decimal("10.03")) == Decimal("10.05")
    f = ticks.tick_table_for(MINI, "FUTURES")
    assert ticks.quantize_tick(f, Decimal("350.03")) == Decimal("350.04")
    assert ticks.is_on_tick_table(f, Decimal("350.04"))
    assert not ticks.is_on_tick_table(f, Decimal("350.03"))
