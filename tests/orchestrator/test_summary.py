import pytest
from helpers_agents import ok_report, ok_turn

from trader.agents.schemas import AnalystReport, DebateTurn
from trader.orchestrator.summary import (
    analyst_consensus,
    conviction_gap,
    research_summary,
    signed_conviction,
    skipped_summary,
)


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


# --- 애널리스트 합의 시 토론 생략 (2026-10-26 전체 검토 L17) -------------------


ROLES = ("technical", "fundamental", "news_macro", "sentiment")


def reports(signals, confidences):
    return [
        AnalystReport(**ok_report(role=r, signal=s, confidence=c))
        for r, s, c in zip(ROLES, signals, confidences, strict=False)
    ]


def test_consensus_all_bullish_high_confidence():
    c = analyst_consensus(reports(["BUY", "STRONG_BUY", "BUY", "BUY"], [0.8] * 4), 0.7)
    assert c is not None and c["direction"] == "BULLISH"
    assert c["min_confidence"] == pytest.approx(0.8) and c["roles"] == list(ROLES)
    s = skipped_summary(c)
    assert s["skipped_debate"] is True and "BULLISH" in s["reason"]
    # 수렴 감시는 계산하지 않는다 (토론이 없으므로)
    assert s["sycophantic"] is None and "gap_round0" not in s and "round0" not in s


def test_consensus_all_bearish():
    c = analyst_consensus(reports(["SELL", "STRONG_SELL", "SELL", "SELL"], [0.9] * 4), 0.7)
    assert c is not None and c["direction"] == "BEARISH"


@pytest.mark.parametrize(
    "signals,confs",
    [
        (["BUY", "BUY", "BUY", "SELL"], [0.8] * 4),  # 하나라도 다르면
        (["BUY", "BUY", "BUY", "NEUTRAL"], [0.8] * 4),  # NEUTRAL 은 합의가 아니다
        (["NEUTRAL"] * 4, [0.9] * 4),
        (["BUY", "BUY", "BUY", "UNKNOWN"], [0.8] * 4),
        (["BUY"] * 4, [0.8, 0.8, 0.8, 0.69]),  # 확신 기준 미달
    ],
)
def test_no_consensus(signals, confs):
    assert analyst_consensus(reports(signals, confs), 0.7) is None


def test_consensus_requires_all_four_roles():
    """리포트가 빠진 역할이 있으면(실패·재시도 소진) 합의로 보지 않는다."""
    assert analyst_consensus(reports(["BUY"] * 3, [0.9] * 3)[:3], 0.7) is None


def _change(frm, reason="새 실적 공시로 판단을 바꿈", evidence=("fundamental.dart",)):
    return {"from_stance": frm, "reason": reason, "evidence": list(evidence)}


def test_justified_flip_is_not_sycophantic():
    """2-L21: 입장을 바꾼 쪽이 바꾼 이유와 리포트에 실제로 있는 근거(source_id)를 대면 아첨이
    아니다."""
    r0 = {"bull": turn("bull", "BULLISH", 0.8), "bear": turn("bear", "BEARISH", 0.7)}
    final = {
        "bull": turn("bull", "BULLISH", 0.6, round=1),
        "bear": turn("bear", "BULLISH", 0.5, round=1, stance_change=_change("BEARISH")),
    }
    s = research_summary(r0, final, rounds=1, evidence_ids={"fundamental.dart"})
    assert s["sycophantic"] is False and s["preferred"] == "final"
    assert s["convergence"] == "justified" and s["convergence_review"] is False


def test_flip_citing_unknown_source_is_sycophantic():
    r0 = {"bull": turn("bull", "BULLISH", 0.8), "bear": turn("bear", "BEARISH", 0.7)}
    final = {
        "bull": turn("bull", "BULLISH", 0.6, round=1),
        "bear": turn(
            "bear", "BULLISH", 0.5, round=1, stance_change=_change("BEARISH", evidence=("x.y",))
        ),
    }
    s = research_summary(r0, final, rounds=1, evidence_ids={"fundamental.dart"})
    assert s["sycophantic"] is True and s["convergence"] == "unjustified"


def test_both_sides_lose_conviction_is_uncertainty_for_review():
    """2-L21: 양측이 입장을 지킨 채 확신만 낮춘 수렴은 불확실성 증가 — 아첨으로 확정하지 않고
    검토 플래그만."""
    r0 = {"bull": turn("bull", "BULLISH", 0.6), "bear": turn("bear", "BEARISH", 0.6)}
    final = {
        "bull": turn("bull", "BULLISH", 0.1, round=1),
        "bear": turn("bear", "BEARISH", 0.1, round=1),
    }
    s = research_summary(r0, final, rounds=1, evidence_ids=set())
    assert s["sycophantic"] is False and s["preferred"] == "final"
    assert s["convergence"] == "uncertainty" and s["convergence_review"] is True


def test_flip_without_verifiable_sources_is_review_not_verdict():
    """근거 목록을 모르면(시장 세션 등) 이유가 있는 전환은 확정 대신 검토 플래그."""
    r0 = {"bull": turn("bull", "BULLISH", 0.8), "bear": turn("bear", "BEARISH", 0.7)}
    final = {
        "bull": turn("bull", "BULLISH", 0.6, round=1),
        "bear": turn("bear", "BULLISH", 0.5, round=1, stance_change=_change("BEARISH")),
    }
    s = research_summary(r0, final, rounds=1)
    assert s["sycophantic"] is False and s["convergence"] == "unverified"
    assert s["convergence_review"] is True


def test_bear_prompt_no_longer_forbids_bullish():
    """2-L21: 약세 리서처의 BULLISH 금지를 역할 설명·출력 규칙 양쪽에서 푼다."""
    from pathlib import Path

    md = (Path(__file__).resolve().parents[2] / ".claude/agents/researcher-bear.md").read_text(
        encoding="utf-8"
    )
    assert "BULLISH 로 바꾸지 않는다" not in md and "stance` 는 BEARISH 또는 NEUTRAL" not in md
    assert "stance_change" in md
