"""Phase 26 단계 3: 파생 스키마 re-export·레지스트리·fo-trader md·오케스트레이터 fo 모드."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from trader.agents import registry, schemas
from trader.agents.registry import agents_json, load_agent, orchestrator_prompt
from trader.config import Settings

REPO_AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"


def test_md_count_16_and_common_rules_prefix():
    """fo-trader 를 더해 md 16개, 공통 규칙 6문장은 괄호 보충 전 접두가 전부 같다."""
    names = list(
        dict.fromkeys(
            [
                *registry.AGENTS,
                *registry.SCREEN_AGENTS,
                *registry.REFLECT_AGENTS,
                *registry.MARKET_AGENTS,
                *registry.QUICK_AGENTS,
                *registry.FO_AGENTS,
                registry.ORCHESTRATOR,
            ]
        )
    )
    assert len(names) == 16 and "fo-trader" in names
    canon = None
    for name in names:
        body = load_agent(name, REPO_AGENTS).prompt
        tail = body.split("# 공통 규칙", 1)[1]
        lines = [ln for ln in tail.splitlines() if re.match(r"^\d\. ", ln)]
        assert len(lines) == 6, name
        prefixes = [ln.split("(", 1)[0].strip().rstrip(".") for ln in lines]
        canon = canon or prefixes
        assert prefixes == canon, name


def test_fo_agents_tuple_and_schema():
    assert registry.FO_AGENTS == (
        "analyst-technical",
        "analyst-sentiment",
        "analyst-market",
        "researcher-bull",
        "researcher-bear",
        "fo-trader",
    )
    assert registry.schema_model_for("fo-trader") is schemas.FoDecisionEnvelope
    j = agents_json(registry.FO_AGENTS, Settings(), REPO_AGENTS)
    assert set(j) == set(registry.FO_AGENTS)
    p = j["fo-trader"]["prompt"]
    assert p.count("스키마:") == 1 and '"expected_version"' in p and '"target_leverage"' in p
    a = load_agent("fo-trader", REPO_AGENTS)
    assert a.model == "opus" and "# 출력 규칙" in a.prompt
    for token in ("HOLD", "FLAT", "max_leverage", "premium_stop_mult", "vs_spot", "stop_pt"):
        assert token in a.prompt, token
    # 체크포인트 이름 → 스키마
    assert schemas.schema_for("_fo_analyst_technical") is schemas.AnalystReport
    assert schemas.schema_for("_fo_analyst_news_macro") is schemas.AnalystReport
    assert schemas.schema_for("_fo_debate_r0_bull") is schemas.DebateTurn
    assert schemas.schema_for("_fo_decision") is schemas.FoDecisionEnvelope
    assert schemas.schema_for("_fo_decision_r2_paper-fo") is schemas.FoDecisionEnvelope
    assert schemas.schema_for("_fo_summary") is None


def test_fo_decision_validators():
    ok = schemas.FoDecision(
        intent="OPEN", direction="LONG", vehicle="FUTURES", target_leverage=1.0, stop_pt=340.0
    )
    assert ok.intent == "OPEN"
    with pytest.raises(ValidationError):
        schemas.FoDecision(intent="OPEN", direction="LONG", vehicle="FUTURES", target_leverage=1)
    with pytest.raises(ValidationError):
        schemas.FoDecision(
            intent="OPEN", direction="FLAT", vehicle="FUTURES", target_leverage=1, stop_pt=1.0
        )
    with pytest.raises(ValidationError):
        schemas.FoDecisionEnvelope(
            decision=ok.model_dump(), request_id="r1", extra_field="x"
        )
    env = schemas.FoDecisionEnvelope(decision=ok.model_dump(), request_id="r1")
    assert env.account_id is None and env.position_id is None


def test_orchestrator_prompt_has_fo_mode_and_schema():
    """kind 별 프롬프트(S3 P0): fo 는 `## fo 모드`, fo_resume 은 `## fo_resume 모드` 절만."""
    p = orchestrator_prompt(Settings(), REPO_AGENTS, kind="fo")
    pr = orchestrator_prompt(Settings(), REPO_AGENTS, kind="fo_resume")
    assert "## fo 모드" in p and "## fo_resume 모드" not in p
    assert "## fo_resume 모드" in pr and "## fo 모드" not in pr
    p = p + pr
    for token in (
        "tool fo bundle --kind analyst",
        "--role technical",
        "_fo_analyst_",
        "tool fo write",
        "tool fo bundle --kind researcher",
        "_fo_debate_r0_",
        "tool fo summarize",
        "tool fo bundle --kind trader",
        "tool submit-fo-decision",
        "FoDecisionEnvelope 스키마",
    ):
        assert token in p, token
    o = load_agent("orchestrator", REPO_AGENTS).prompt
    assert "position" in o.split("## fo_resume 모드", 1)[1].split("## ", 1)[0]
