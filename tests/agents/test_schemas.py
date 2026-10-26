import json

import pytest
from helpers_agents import ok_decision, ok_report, ok_turn
from pydantic import ValidationError

from trader.agents import schemas
from trader.agents.schemas import AnalystReport, DebateTurn, Decision, json_schema, schema_for


def test_analyst_report_bounds_and_forbid():
    AnalystReport(**ok_report())
    with pytest.raises(ValidationError):
        AnalystReport(**ok_report(score=1.5))
    with pytest.raises(ValidationError):
        AnalystReport(**ok_report(bullets=["x"] * 8))
    with pytest.raises(ValidationError):
        AnalystReport(**ok_report(summary="가" * 301))
    with pytest.raises(ValidationError):
        AnalystReport(**ok_report(extra="no"))
    with pytest.raises(ValidationError):
        AnalystReport(**ok_report(signal="MAYBE"))


def test_debate_turn_bounds():
    DebateTurn(**ok_turn())
    with pytest.raises(ValidationError):
        DebateTurn(**ok_turn(conviction=1.2))
    with pytest.raises(ValidationError):
        DebateTurn(**ok_turn(arguments=["x"] * 6))
    with pytest.raises(ValidationError):
        DebateTurn(**ok_turn(timing={"action": "LATER"}))


def test_decision_weight_cap_and_defaults():
    d = Decision(**ok_decision())
    assert d.memory_ids_used == []
    with pytest.raises(ValidationError):
        Decision(**ok_decision(target_weight=0.3))
    with pytest.raises(ValidationError):
        Decision(**ok_decision(rationale="가" * 501))
    with pytest.raises(ValidationError):
        Decision(**ok_decision(horizon_days=0))


def test_schema_for_names():
    assert schema_for("10_analyst_technical") is AnalystReport
    assert schema_for("10_analyst_news_macro") is AnalystReport
    assert schema_for("20_debate_r1_bear") is DebateTurn
    assert schema_for("30_decision") is Decision
    assert schema_for("25_research_summary") is None
    assert schema_for("00_inputs") is None


def test_json_schema_compact():
    s = json_schema(DebateTurn)
    assert "\n" not in s and ": " not in s and '"conviction"' in s
    assert json.loads(s)["additionalProperties"] is False


def test_call_meta():
    m = schemas.CallMeta(
        agent="trader", model="opus", session_id=None, cost_usd=None, duration_ms=None
    )
    assert m.model_dump()["agent"] == "trader"


def test_decision_prices_must_be_positive():
    """전체 검토 O16: 음수·0 가격은 스키마에서 거부."""
    import pytest
    from helpers_agents import ok_decision
    from pydantic import ValidationError

    from trader.agents.schemas import Decision

    for field in ("stop_loss", "take_profit", "limit_price"):
        for bad in (-1.0, 0.0):
            with pytest.raises(ValidationError):
                Decision(**ok_decision(**{field: bad}))
    assert Decision(**ok_decision(stop_loss=None, take_profit=None)).stop_loss is None
