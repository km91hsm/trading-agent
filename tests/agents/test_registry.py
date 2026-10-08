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
    ):
        assert token in p, token


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
