"""리서치 토론 수렴 감시와 요약 병합 (스펙 §4.3).

부호 있는 확신 = conviction × {BULLISH:+1, NEUTRAL:0, BEARISH:-1}.
양측 차이(gap)가 라운드 0 에선 0.2 를 넘었는데 최종 라운드에서 0.2 이하로 붕괴하면
"아첨적 수렴" 으로 보고 트레이더에 라운드 0 우선(preferred=round0)을 알린다.
"""

from __future__ import annotations

from trader.agents.schemas import DebateTurn

CONVERGENCE_GAP = 0.2
_SIGN = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}


def signed_conviction(turn: DebateTurn) -> float:
    return turn.conviction * _SIGN[turn.stance]


def conviction_gap(bull: DebateTurn, bear: DebateTurn) -> float:
    return abs(signed_conviction(bull) - signed_conviction(bear))


def research_summary(
    round0: dict[str, DebateTurn], final: dict[str, DebateTurn], rounds: int
) -> dict:
    """라운드 0 + 최종 + 쟁점 + 수렴 플래그. 양측 모두 있어야 한다."""
    for d in (round0, final):
        if "bull" not in d or "bear" not in d:
            raise ValueError("bull·bear 양측 턴이 모두 필요합니다")
    gap0 = conviction_gap(round0["bull"], round0["bear"])
    gap_final = conviction_gap(final["bull"], final["bear"])
    sycophantic = rounds > 0 and gap_final <= CONVERGENCE_GAP < gap0
    return {
        "rounds": rounds,
        "round0": {s: t.model_dump() for s, t in round0.items()},
        "final": {s: t.model_dump() for s, t in final.items()},
        "gap_round0": round(gap0, 4),
        "gap_final": round(gap_final, 4),
        "timing_agree": final["bull"].timing.action == final["bear"].timing.action,
        "sycophantic": sycophantic,
        "preferred": "round0" if sycophantic else "final",
        "issues": list(final["bull"].rebuttals) + list(final["bear"].rebuttals),
    }
