from pathlib import Path

import pytest

from trader.agents import registry
from trader.agents.registry import AGENTS, agents_json, load_agent, orchestrator_prompt
from trader.config import ConfigError, Settings

REPO_AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"
COMMON = ["제공된 데이터만", "수치", "data_gaps", "UNKNOWN", "검증", "한국어"]


def test_all_agents_load_with_common_rules():
    assert set(AGENTS) == {
        "analyst-technical",
        "analyst-fundamental",
        "analyst-news-macro",
        "analyst-sentiment",
        "researcher-bull",
        "researcher-bear",
        "trader",
    }
    for name in AGENTS:
        a = load_agent(name, REPO_AGENTS)
        assert a.name == name and a.description and a.model in ("haiku", "sonnet", "opus")
        assert not a.prompt.startswith("---")
        for phrase in COMMON:
            assert phrase in a.prompt, (name, phrase)
        assert "# 출력 규칙" in a.prompt and '"type":"object"' in a.prompt  # 스키마 전문 포함


def test_role_specific_rules():
    assert "악마의 변호인" in load_agent("researcher-bear", REPO_AGENTS).prompt
    t = load_agent("trader", REPO_AGENTS).prompt
    assert "HOLD" in t and "round0" in t and "30bp" in t and "LIMIT" in t
    assert "부정" in load_agent("analyst-news-macro", REPO_AGENTS).prompt


def test_agents_json_uses_settings_models():
    s = Settings(models={"analyst": "haiku", "researcher_bear": "opus", "trader": "sonnet"})
    j = agents_json(AGENTS, s, REPO_AGENTS)
    assert set(j) == set(AGENTS)
    assert j["analyst-technical"]["model"] == "haiku"
    assert j["researcher-bear"]["model"] == "opus"
    assert j["trader"]["model"] == "sonnet"
    assert j["researcher-bull"]["model"] == load_agent("researcher-bull", REPO_AGENTS).model
    assert j["trader"]["tools"] == [] and j["trader"]["prompt"] and j["trader"]["description"]


def test_orchestrator_prompt_has_procedure():
    p = orchestrator_prompt(Settings(), REPO_AGENTS)
    for token in (
        "tool status",
        "tool collect",
        "tool checkpoint list",
        "tool checkpoint write",
        "tool checkpoint summarize",
        "tool submit-decision",
        "10_analyst_",
        "20_debate_r0_bull",
        "debate_rounds=1",
        "그대로 붙여",  # 도구 JSON 출력을 서브에이전트 프롬프트에 전문 그대로 넣는다 (실기 반영)
        "요약하거나 생략하지",
        "요약·발췌하지 않는다",  # 리서처·트레이더 입력도 전문 (리뷰 I1)
        "--asof",
        "tool checkpoint bundle --kind researcher",  # 파이썬이 조립한 묶음을 전문 전달 (실기 3차)
        "tool checkpoint bundle --kind trader",
    ):
        assert token in p, token
    assert "anonymize=" not in p


def test_missing_agent_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_agent("nope", tmp_path)


def test_frontmatter_parse(tmp_path):
    (tmp_path / "x.md").write_text(
        "---\nname: x\ndescription: 설명\nmodel: sonnet\ntools: []\n---\n# 역할\n본문\n",
        encoding="utf-8",
    )
    a = registry.load_agent("x", tmp_path)
    assert a.description == "설명" and a.prompt.startswith("# 역할")


def test_agents_json_appends_schema_when_md_lacks_it():
    """서브에이전트 프롬프트는 md 본문뿐이다. screener·reflector md 에 스키마가 없으면 런타임에
    정본(schemas.py) 스키마를 덧붙인다 — 트레이더처럼 md 에 이미 있으면 두 번 넣지 않는다."""
    j = agents_json(("screener", "reflector", "trader"), Settings(), REPO_AGENTS)
    for n in ("screener", "reflector", "trader"):
        assert '"type":"object"' in j[n]["prompt"], n
        assert j[n]["prompt"].count("스키마:") == 1, n
    assert '"picks"' in j["screener"]["prompt"] and '"signal_review"' in j["reflector"]["prompt"]


def test_common_rules_identical_prefix_across_all_agents():
    """공통 규칙 6문장은 괄호 보충 전까지 모든 md(16개)에서 같아야 한다."""
    import re

    names = list(
        dict.fromkeys(
            [
                *AGENTS,
                *registry.SCREEN_AGENTS,
                *registry.REFLECT_AGENTS,
                *registry.MARKET_AGENTS,
                *registry.QUICK_AGENTS,
                *registry.FO_AGENTS,
                registry.ORCHESTRATOR,
            ]
        )
    )
    assert len(names) == 16  # + briefing(2026-10-27) + fo-trader(Phase 26)
    canon = None
    for name in names:
        body = load_agent(name, REPO_AGENTS).prompt
        tail = body.split("# 공통 규칙", 1)[1]
        lines = [ln for ln in tail.splitlines() if re.match(r"^\d\. ", ln)]
        assert len(lines) == 6, name
        prefixes = [ln.split("(", 1)[0].strip().rstrip(".") for ln in lines]
        canon = canon or prefixes
        assert prefixes == canon, name


def test_market_agents_and_hedge_proposal_schema():
    """시장 세션(Phase 18): 에이전트 4개, strategist 에 HedgeProposal 스키마 1회 부착,
    오케스트레이터 프롬프트에 market 모드 절·스키마 줄."""
    assert registry.MARKET_AGENTS == (
        "analyst-market",
        "researcher-bull",
        "researcher-bear",
        "strategist",
    )
    j = agents_json(registry.MARKET_AGENTS, Settings(), REPO_AGENTS)
    assert set(j) == set(registry.MARKET_AGENTS)
    assert '"hedge_weight"' in j["strategist"]["prompt"]
    assert j["strategist"]["prompt"].count("스키마:") == 1
    assert j["analyst-market"]["model"] == "sonnet"  # md 기본, settings.models.analyst 가 우선
    p = orchestrator_prompt(Settings(), REPO_AGENTS, kind="market")
    assert "## market 모드" in p and "HedgeProposal 스키마" in p
    assert "submit-market-view" in p


def test_scout_agent_and_discovered_schema():
    """Phase 19 발굴: screen 세션은 scout → screener 두 단계, scout 에 Discovered 스키마 부착."""
    assert registry.SCREEN_AGENTS == ("scout", "screener")
    j = agents_json(registry.SCREEN_AGENTS, Settings(), REPO_AGENTS)
    assert '"candidates"' in j["scout"]["prompt"] and '"tier"' in j["scout"]["prompt"]
    assert j["scout"]["prompt"].count("스키마:") == 1
    assert "유니버스" in j["scout"]["prompt"] and "max_listed" in j["scout"]["prompt"]
    assert "discovery" in load_agent("screener", REPO_AGENTS).prompt
    o = load_agent("orchestrator", REPO_AGENTS).prompt
    assert "tool scan discovery-inputs" in o and "submit-discovery" in o
