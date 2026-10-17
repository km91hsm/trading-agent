import pytest
from helpers_agents import ok_turn

from trader.agents.schemas import DebateTurn
from trader.orchestrator.summary import conviction_gap, research_summary, signed_conviction


def turn(side, stance, conviction, round=0, **over):
    return DebateTurn(**ok_turn(side, round=round, stance=stance, conviction=conviction, **over))


def test_signed_conviction_and_gap():
    assert signed_conviction(turn("bull", "BULLISH", 0.8)) == 0.8
    assert signed_conviction(turn("bear", "BEARISH", 0.7)) == -0.7
    assert signed_conviction(turn("bear", "NEUTRAL", 0.9)) == 0.0
    assert conviction_gap(
        turn("bull", "BULLISH", 0.8), turn("bear", "BEARISH", 0.7)
    ) == pytest.approx(1.5)


def test_sycophantic_convergence():
    r0 = {"bull": turn("bull", "BULLISH", 0.8), "bear": turn("bear", "BEARISH", 0.7)}
    final = {
        "bull": turn("bull", "BULLISH", 0.6, round=1, rebuttals=["반박A"]),
        "bear": turn("bear", "BULLISH", 0.5, round=1, rebuttals=["반박B"]),  # bear 가 동조
    }
    s = research_summary(r0, final, rounds=1)
    assert s["gap_round0"] == pytest.approx(1.5) and s["gap_final"] == pytest.approx(0.1)
    assert s["sycophantic"] is True and s["preferred"] == "round0"
    assert s["issues"] == ["반박A", "반박B"] and s["timing_agree"] is True
    assert s["round0"]["bear"]["stance"] == "BEARISH"


def test_independent_agreement_not_sycophantic():
    """라운드 0 부터 양측이 가까우면 수렴이 아니라 독립적 합의다."""
    r0 = {"bull": turn("bull", "NEUTRAL", 0.5), "bear": turn("bear", "NEUTRAL", 0.6)}
    final = {
        "bull": turn("bull", "NEUTRAL", 0.5, round=1),
        "bear": turn("bear", "NEUTRAL", 0.6, round=1),
    }
    s = research_summary(r0, final, rounds=1)
    assert s["sycophantic"] is False and s["preferred"] == "final"


def test_rounds_zero():
    r0 = {"bull": turn("bull", "BULLISH", 0.8), "bear": turn("bear", "BEARISH", 0.7)}
    s = research_summary(r0, r0, rounds=0)
    assert s["rounds"] == 0 and s["sycophantic"] is False and s["issues"] == []


def test_requires_both_sides():
    with pytest.raises(ValueError):
        research_summary({"bull": turn("bull", "BULLISH", 0.8)}, {}, rounds=0)
