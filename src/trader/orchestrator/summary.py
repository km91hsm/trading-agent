"""리서치 토론 수렴 감시와 요약 병합 (스펙 §4.3).

부호 있는 확신 = conviction × {BULLISH:+1, NEUTRAL:0, BEARISH:-1}.
양측 차이(gap)가 라운드 0 에선 0.2 를 넘었는데 최종 라운드에서 0.2 이하로 붕괴하면
"아첨적 수렴" 으로 보고 트레이더에 라운드 0 우선(preferred=round0)을 알린다.

애널리스트 4역할이 모두 같은 방향·높은 확신이면 토론을 생략한다(전체 검토 L17):
`analyst_consensus` 가 판정하고 `skipped_summary` 가 수렴 감시 없는 요약을 만든다.
"""

from __future__ import annotations

from trader.agents.schemas import AnalystReport, DebateTurn

CONVERGENCE_GAP = 0.2
_SIGN = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}
CONSENSUS_ROLES = ("technical", "fundamental", "news_macro", "sentiment")
# 애널리스트 신호 → 방향. NEUTRAL·UNKNOWN 은 방향이 없어 합의로 치지 않는다.
_DIRECTION = {
    "STRONG_BUY": "BULLISH",
    "BUY": "BULLISH",
    "SELL": "BEARISH",
    "STRONG_SELL": "BEARISH",
}


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


def analyst_consensus(reports: list[AnalystReport], min_conviction: float) -> dict | None:
    """네 역할 리포트가 모두 같은 방향이고 확신이 전부 기준 이상이면 합의 정보, 아니면 None.

    역할 하나라도 빠지면(실패·재시도 소진) 합의로 보지 않는다 — 토론을 건너뛸 근거가 약하다."""
    by_role = {r.role: r for r in reports}
    if any(role not in by_role for role in CONSENSUS_ROLES):
        return None
    picked = [by_role[role] for role in CONSENSUS_ROLES]
    directions = {_DIRECTION.get(r.signal) for r in picked}
    if len(directions) != 1 or None in directions:
        return None
    min_conf = min(r.confidence for r in picked)
    if min_conf < min_conviction:
        return None
    return {
        "direction": directions.pop(),
        "signals": {r.role: r.signal for r in picked},
        "min_confidence": round(min_conf, 4),
        "threshold": min_conviction,
        "roles": list(CONSENSUS_ROLES),
    }


def skipped_summary(consensus: dict) -> dict:
    """토론을 생략한 25_research_summary. 토론 턴이 없으니 수렴 감시는 계산하지 않는다."""
    return {
        "rounds": 0,
        "skipped_debate": True,
        "reason": (
            f"애널리스트 {len(consensus['roles'])}명 모두 {consensus['direction']} · "
            f"최저 확신 {consensus['min_confidence']:.2f} ≥ {consensus['threshold']:.2f}"
        ),
        "consensus": consensus,
        "sycophantic": None,
        "preferred": None,
    }
