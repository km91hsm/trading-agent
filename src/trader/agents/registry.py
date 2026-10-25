"""에이전트 정의 레지스트리.

.claude/agents/*.md 를 읽어 `claude --agents` JSON 과 오케스트레이터 프롬프트를 만든다.
md 형식: YAML 프런트매터(name, description, model, tools) + 마크다운 본문.
모델은 settings.models 가 우선한다 (`analyst-*` → `analyst`, 그 외는 `-` → `_`).
공통 규칙 6문장은 md 마다 같은 문장으로 들어 있다 (tests/agents/test_registry.py 가 확인).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from trader.agents import schemas
from trader.config import ConfigError, Settings
from trader.platform import paths

AGENTS: tuple[str, ...] = (
    "analyst-technical",
    "analyst-fundamental",
    "analyst-news-macro",
    "analyst-sentiment",
    "researcher-bull",
    "researcher-bear",
    "trader",
)
ORCHESTRATOR = "orchestrator"
# Phase 19: scout(뉴스 발굴) → screener(선정) — 같은 screen 세션 안의 두 단계
SCREEN_AGENTS: tuple[str, ...] = ("scout", "screener")
# Phase 23 장중 발굴: 뉴스 수집 뒤 scout 만 (screener 없음, 후보는 파이썬이 분석 큐로)
DISCOVER_AGENTS: tuple[str, ...] = ("scout",)
REFLECT_AGENTS: tuple[str, ...] = ("reflector",)
# Phase 18 시장 세션: 거시 애널리스트 1 + 리서처 토론 + 전략가(헤지 비중 제안)
MARKET_AGENTS: tuple[str, ...] = (
    "analyst-market",
    "researcher-bull",
    "researcher-bear",
    "strategist",
)


@dataclass
class Agent:
    name: str
    description: str
    model: str
    prompt: str


def agents_dir(base: Path | None = None) -> Path:
    return base if base is not None else paths.agents_dir()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}, text
    head = parts[0].lstrip("-").strip()
    body = parts[1].lstrip("\n")
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"에이전트 프런트매터 구문 오류: {e}") from e
    return (meta if isinstance(meta, dict) else {}), body


def load_agent(name: str, base: Path | None = None) -> Agent:
    path = agents_dir(base) / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"에이전트 정의 없음: {path}") from None
    meta, body = _split_frontmatter(text)
    return Agent(
        name=str(meta.get("name") or name),
        description=str(meta.get("description") or ""),
        model=str(meta.get("model") or "sonnet"),
        prompt=body.strip() + "\n",
    )


def model_for(name: str, settings: Settings, default: str) -> str:
    key = "analyst" if name.startswith("analyst-") else name.replace("-", "_")
    return settings.models.get(key, default)


def schema_model_for(name: str) -> type | None:
    """에이전트가 출력하는 스키마의 정본 모델."""
    if name.startswith("analyst-"):
        return schemas.AnalystReport
    if name.startswith("researcher-"):
        return schemas.DebateTurn
    return {
        "trader": schemas.Decision,
        "screener": schemas.Screened,
        "scout": schemas.Discovered,
        "reflector": schemas.Reflection,
        "strategist": schemas.HedgeProposal,
    }.get(name)


def _with_schema(name: str, prompt: str) -> str:
    """md 에 스키마 전문이 없으면 정본(schemas.py)에서 만들어 덧붙인다 (md 사본은 설명용)."""
    model = schema_model_for(name)
    if model is None or '"type":"object"' in prompt:
        return prompt
    return prompt.rstrip("\n") + "\n\n스키마: " + schemas.json_schema(model) + "\n"


def agents_json(names: tuple[str, ...], settings: Settings, base: Path | None = None) -> dict:
    """`claude --agents <json>` 형식: {name: {description, prompt, tools, model}}."""
    out: dict[str, dict] = {}
    for n in names:
        a = load_agent(n, base)
        out[n] = {
            "description": a.description,
            "prompt": _with_schema(n, a.prompt),
            "tools": [],
            "model": model_for(n, settings, a.model),
        }
    return out


def orchestrator_prompt(settings: Settings, base: Path | None = None) -> str:
    """orchestrator.md 본문 + 런타임 설정(토론 라운드·스키마 요약).

    md 의 `python -m trader tool` 은 실제 도구 명령(`bundle.tool_command()`)으로 바꾼다
    (번들은 `trader tool`) — `--allowedTools` 와 같은 문자열이어야 LLM 이 도구를 부를 수 있다."""
    from trader.platform import bundle

    body = load_agent(ORCHESTRATOR, base).prompt.replace(
        "python -m trader tool", bundle.tool_command()
    )
    runtime = (
        "\n# 런타임 설정\n"
        f"- debate_rounds={settings.debate_rounds} (라운드 0 독립 의견 뒤 반박 라운드 수)\n"
        "- 체크포인트 이름: 10_analyst_<technical|fundamental|news_macro|sentiment>, "
        "20_debate_r<n>_<bull|bear>, 25_research_summary(파이썬 생성), 30_decision\n"
        "- AnalystReport 스키마: " + schemas.json_schema(schemas.AnalystReport) + "\n"
        "- DebateTurn 스키마: " + schemas.json_schema(schemas.DebateTurn) + "\n"
        "- Decision 스키마: " + schemas.json_schema(schemas.Decision) + "\n"
        "- Screened 스키마(screen 모드): " + schemas.json_schema(schemas.Screened) + "\n"
        "- Reflection 스키마(reflect 모드, 배열로 제출): "
        + schemas.json_schema(schemas.Reflection)
        + "\n"
        "- HedgeProposal 스키마(market 모드, strategist 출력): "
        + schemas.json_schema(schemas.HedgeProposal)
        + "\n"
    )
    return body + runtime
