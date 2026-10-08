"""설정 로더 5개: settings / accounts / sources / universe / secrets.

yaml 모델은 전부 extra="forbid" 라 yaml 키를 추가하면 모델도 같이 바꾼다.
검증·파일 오류는 모두 ConfigError 로 통일한다.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trader.platform import paths

Env = Literal["paper", "real"]
Mode = Literal["auto", "approve", "off"]
Market = Literal["KR", "US"]


class ConfigError(Exception):
    """설정 파일 읽기·검증 실패."""


# --- settings.yaml ---------------------------------------------------------


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: float = 0.25
    max_positions: int = 5
    daily_loss_stop_pct: float = -0.05
    stop_loss_pct: float = -0.15
    min_cash_pct: float = 0.0


class LlmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = 2.0
    timeout_s: int = 600
    max_parallel: int = 4
    daily_session_cap: int = 12


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    port: int = 8787


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[Market] = Field(default_factory=lambda: ["KR", "US"])
    risk: RiskLimits = Field(default_factory=RiskLimits)
    models: dict[str, str] = Field(default_factory=dict)
    debate_rounds: int = Field(default=1, ge=0, le=2)
    anonymize: bool = False
    claude_bin: str | None = None
    llm: LlmSettings = Field(default_factory=LlmSettings)
    web: WebSettings = Field(default_factory=WebSettings)


# --- accounts.yaml ---------------------------------------------------------


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    env: Env
    cano: str
    acnt_prdt_cd: str = "01"
    markets: list[Market] = Field(default_factory=lambda: ["KR", "US"])
    mode: Mode
    initial_capital: Decimal | None = None


class _AccountsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountConfig] = Field(default_factory=list)


# --- sources.yaml ----------------------------------------------------------


class NewsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    google_rss: bool = True
    naver: bool = True
    max_items: int = 20
    macro_items: int = 10


class MacroCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    fred: bool = True
    ecos: bool = True


class ToggleCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class EdgarCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    user_agent: str


class CommunityCfg(BaseModel):
    """커뮤니티 소스 기본값은 스펙 §0.3 정책: robots·약관 허용만 on."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reddit: bool = True
    clien: bool = True
    dcinside: bool = False
    fmkorea: bool = False
    toss: bool = False
    naver_board: bool = False
    max_posts: int = 50


class ChannelsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    youtube: list[str] = Field(default_factory=list)
    telegram: list[str] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    news: NewsCfg = Field(default_factory=NewsCfg)
    macro: MacroCfg = Field(default_factory=MacroCfg)
    dart: ToggleCfg = Field(default_factory=ToggleCfg)
    edgar: EdgarCfg
    community: CommunityCfg = Field(default_factory=CommunityCfg)
    channels: ChannelsCfg = Field(default_factory=ChannelsCfg)
    disabled_sites: list[str] = Field(default_factory=list)


# --- universe.yaml ---------------------------------------------------------


class UsSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange: Literal["NAS", "NYS", "AMS"]


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kr: list[str] = Field(default_factory=list)
    us: list[UsSymbol] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)


# --- secrets/.env ----------------------------------------------------------


class Secrets(BaseModel):
    """필드명 = 환경변수명. 비어 있지 않은 환경변수가 .env 값을 덮어쓴다."""

    model_config = ConfigDict(extra="ignore")

    KIS_PAPER_APP_KEY: str | None = None
    KIS_PAPER_APP_SECRET: str | None = None
    KIS_REAL_APP_KEY: str | None = None
    KIS_REAL_APP_SECRET: str | None = None
    DART_API_KEY: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None
    NAVER_CLIENT_ID: str | None = None
    NAVER_CLIENT_SECRET: str | None = None
    FRED_API_KEY: str | None = None
    ECOS_API_KEY: str | None = None
    WEB_TOKEN: str | None = None


# --- 로더 -------------------------------------------------------------------


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(루트)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"{path.name} 읽기 실패: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 의 최상위는 매핑이어야 합니다")
    return data


def _validate(model, data: dict, name: str):
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{name} 검증 실패: {_describe(e)}") from e


def load_settings(path: Path | None = None) -> Settings:
    p = path or paths.config_dir() / "settings.yaml"
    return _validate(Settings, _read_yaml(p), p.name)


def load_accounts(path: Path | None = None) -> list[AccountConfig]:
    p = path or paths.config_dir() / "accounts.yaml"
    accounts = _validate(_AccountsFile, _read_yaml(p), p.name).accounts
    seen: set[str] = set()
    for a in accounts:
        if a.id in seen:
            raise ConfigError(f"{p.name}: 계좌 id 중복: {a.id}")
        seen.add(a.id)
        if a.env == "real" and a.mode == "auto":
            raise ConfigError(
                f"{p.name}: 실전 계좌 '{a.id}' 는 yaml 에서 auto 로 둘 수 없습니다 "
                "(런타임 control 에서 확인 문자열로만 전환)"
            )
    return accounts


def load_sources(path: Path | None = None) -> SourcesConfig:
    p = path or paths.config_dir() / "sources.yaml"
    return _validate(SourcesConfig, _read_yaml(p), p.name)


def load_universe(path: Path | None = None) -> Universe:
    p = path or paths.config_dir() / "universe.yaml"
    return _validate(Universe, _read_yaml(p), p.name)


def load_secrets(path: Path | None = None) -> Secrets:
    p = path or paths.secrets_path()
    values: dict[str, str | None] = {}
    if p.exists():
        values.update(dotenv_values(p, encoding="utf-8"))
    for key in Secrets.model_fields:
        env = os.environ.get(key)
        if env:
            values[key] = env
    return _validate(Secrets, {k: v for k, v in values.items() if v}, p.name)
