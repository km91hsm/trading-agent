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


@dataclass
class Agent:
    name: str
    description: str
    model: str
    prompt: str


def agents_dir(base: Path | None = None) -> Path:
    return base if base is not None else paths.project_root() / ".claude" / "agents"


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


def agents_json(names: tuple[str, ...], settings: Settings, base: Path | None = None) -> dict:
    """`claude --agents <json>` 형식: {name: {description, prompt, tools, model}}."""
    out: dict[str, dict] = {}
    for n in names:
        a = load_agent(n, base)
        out[n] = {
            "description": a.description,
            "prompt": a.prompt,
            "tools": [],
            "model": model_for(n, settings, a.model),
        }
    return out


def orchestrator_prompt(settings: Settings, base: Path | None = None) -> str:
    """orchestrator.md 본문 + 런타임 설정(토론 라운드·스키마 요약)."""
    body = load_agent(ORCHESTRATOR, base).prompt
    runtime = (
        "\n# 런타임 설정\n"
        f"- debate_rounds={settings.debate_rounds} (라운드 0 독립 의견 뒤 반박 라운드 수)\n"
        "- 체크포인트 이름: 10_analyst_<technical|fundamental|news_macro|sentiment>, "
        "20_debate_r<n>_<bull|bear>, 25_research_summary(파이썬 생성), 30_decision\n"
        "- AnalystReport 스키마: " + schemas.json_schema(schemas.AnalystReport) + "\n"
        "- DebateTurn 스키마: " + schemas.json_schema(schemas.DebateTurn) + "\n"
        "- Decision 스키마: " + schemas.json_schema(schemas.Decision) + "\n"
    )
    return body + runtime
