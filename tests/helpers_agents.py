"""에이전트 테스트 공용 헬퍼: 스키마를 통과하는 최소 페이로드."""

from __future__ import annotations


def ok_report(**over) -> dict:
    d = {
        "ticker": "005930",
        "asof": "2026-10-09T06:00:00+00:00",
        "role": "technical",
        "summary": "추세 양호",
        "bullets": ["20일선 위", "거래량 증가"],
        "signal": "BUY",
        "score": 0.4,
        "confidence": 0.6,
        "horizon": "short",
        "key_risks": ["단기 과열"],
        "counter_evidence": [],
        "evidence": [{"claim": "종가 > SMA20", "source_id": "technical.indicators"}],
        "data_gaps": [],
    }
    d.update(over)
    return d


def ok_turn(side="bull", round=0, **over) -> dict:
    d = {
        "side": side,
        "round": round,
        "stance": "BULLISH" if side == "bull" else "BEARISH",
        "valuation_view": "UNDERVALUED" if side == "bull" else "OVERVALUED",
        "arguments": ["논거1"],
        "rebuttals": [],
        "timing": {"action": "NOW", "condition": None, "price_zone": "70,000~72,000"},
        "conviction": 0.7,
        "what_would_change_my_mind": ["실적 쇼크"],
    }
    d.update(over)
    return d


def ok_decision(**over) -> dict:
    d = {
        "ticker": "005930",
        "action": "BUY",
        "target_weight": 0.1,
        "order_type": "LIMIT",
        "limit_price": 71000.0,
        "stop_loss": 65000.0,
        "take_profit": 80000.0,
        "horizon_days": 10,
        "confidence": 0.6,
        "rationale": "검증가능한 근거 요약",
        "invalidation_conditions": ["20일선 이탈"],
        "memory_ids_used": [],
    }
    d.update(over)
    return d
