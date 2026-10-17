"""설정 로더 5개: settings / accounts / sources / universe / secrets.

yaml 모델은 전부 extra="forbid" 라 yaml 키를 추가하면 모델도 같이 바꾼다.
검증·파일 오류는 모두 ConfigError 로 통일한다.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

    budget_usd: float = 5.0
    timeout_s: int = 600
    max_parallel: int = 4
    daily_session_cap: int = 12


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    port: int = 8787


class HalfLives(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: int = 14
    mid: int = 90
    long: int = 365


class MemorySettings(BaseModel):
    """메모리 검색 설정 (스펙 §6.5)."""

    model_config = ConfigDict(extra="forbid")

    half_lives: HalfLives = Field(default_factory=HalfLives)
    warmup: int = 50
    drawdown_stop_pct: float = -0.10
    vix_spike_pct: float = 0.30
    top_k: int = 5


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
    memory: MemorySettings = Field(default_factory=MemorySettings)


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
    max_videos: int = Field(default=3, ge=1, le=20)
    max_posts: int = Field(default=30, ge=1, le=200)


class KisCfg(BaseModel):
    """daily_days 는 high_52w_pos(252봉)·sma_120 워밍업을 위해 260 이상이어야 한다."""

    model_config = ConfigDict(extra="forbid")

    daily_days: int = 260
    investor_days: int = 20


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kis: KisCfg = Field(default_factory=KisCfg)
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
    REDDIT_CLIENT_ID: str | None = None
    REDDIT_CLIENT_SECRET: str | None = None
    REDDIT_USER: str | None = None


# --- 로더 -------------------------------------------------------------------


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(루트)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _read_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path.name} 은 UTF-8 로 저장해야 합니다 (cp949 등 감지): {e}") from e
    except OSError as e:
        raise ConfigError(f"{path.name} 읽기 실패: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} 구문 오류: {e}") from e
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


# --- schedule.yaml (Phase 6 데몬) ---------------------------------------------

_HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _check_hm(value: str) -> str:
    if not _HM_RE.match(value):
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {value!r}")
    return value


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


class MonitorWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    interval_s: int = Field(default=60, ge=5, le=3600)

    @field_validator("start", "end")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_hm(v)

    def start_hm(self) -> tuple[int, int]:
        return _hm(self.start)

    def end_hm(self) -> tuple[int, int]:
        return _hm(self.end)


class MarketSchedule(BaseModel):
    """시장별 시각. tz 는 그 시장의 시간대(기본 KST; US 는 America/New_York 권장)."""

    model_config = ConfigDict(extra="forbid")

    scan: str
    analyze: str
    monitor: MonitorWindow
    tz: str = "Asia/Seoul"

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"알 수 없는 시간대: {v!r}") from None
        return v

    @field_validator("scan", "analyze")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_hm(v)

    def scan_hm(self) -> tuple[int, int]:
        return _hm(self.scan)

    def analyze_hm(self) -> tuple[int, int]:
        return _hm(self.analyze)


class WeeklyJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: str
    time: str

    @field_validator("weekday")
    @classmethod
    def _w(cls, v: str) -> str:
        if v not in WEEKDAYS:
            raise ValueError(f"요일은 {WEEKDAYS} 중 하나여야 합니다: {v!r}")
        return v

    @field_validator("time")
    @classmethod
    def _t(cls, v: str) -> str:
        return _check_hm(v)


class ScheduleConfig(BaseModel):
    """config/schedule.yaml (KST). 데몬이 잡을 등록할 때 읽는다."""

    model_config = ConfigDict(extra="forbid")

    kr: MarketSchedule
    us: MarketSchedule
    reflect: str
    ml_train: WeeklyJob

    @field_validator("reflect")
    @classmethod
    def _r(cls, v: str) -> str:
        return _check_hm(v)


# --- ml.yaml (Phase 7) ----------------------------------------------------------


class LabelThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    up: float = 0.02
    down: float = -0.02


class LabelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon_days: int = Field(default=5, ge=1, le=60)
    thresholds: LabelThresholds = Field(default_factory=LabelThresholds)
    embargo_days: int = Field(default=5, ge=0, le=60)


class TrainCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_years: int = Field(default=3, ge=1, le=10)
    walk_forward_folds: int = Field(default=6, ge=2, le=20)


class QuantModelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    promote_min_accuracy: float = 0.40
    promote_min_alpha: float = 0.0
    backend: Literal["auto", "sklearn", "lightgbm"] = "sklearn"  # 플랫폼 간 동일 동작을 위해 고정
    min_up_predictions: int = 5  # 폴드마다 up 예측 최소 건수 (알파 추정 노이즈 방지)


class RankerCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class CalibrationCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_decisions: int = 60


class RlSizerCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    seeds: int = 3


class MlModelsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quant: QuantModelCfg = Field(default_factory=QuantModelCfg)
    ranker: RankerCfg = Field(default_factory=RankerCfg)
    calibration: CalibrationCfg = Field(default_factory=CalibrationCfg)
    rl_sizer: RlSizerCfg = Field(default_factory=RlSizerCfg)


class MlConfig(BaseModel):
    """config/ml.yaml (스펙 §9). 없으면 기본값."""

    model_config = ConfigDict(extra="forbid")

    label: LabelCfg = Field(default_factory=LabelCfg)
    train: TrainCfg = Field(default_factory=TrainCfg)
    models: MlModelsCfg = Field(default_factory=MlModelsCfg)


def load_ml(path: Path | None = None) -> MlConfig:
    p = path or paths.config_dir() / "ml.yaml"
    return _validate(MlConfig, _read_yaml(p), p.name)


def load_schedule(path: Path | None = None) -> ScheduleConfig:
    p = path or paths.config_dir() / "schedule.yaml"
    if not p.exists():
        raise ConfigError(f"스케줄 파일이 없습니다: {p}")
    return _validate(ScheduleConfig, _read_yaml(p), p.name)


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
        try:
            values.update(dotenv_values(p, encoding="utf-8"))
        except UnicodeDecodeError as e:
            raise ConfigError(f"{p.name} 은 UTF-8 로 저장해야 합니다 (cp949 등 감지): {e}") from e
    for key in Secrets.model_fields:
        env = (os.environ.get(key) or "").strip()
        if env:
            values[key] = env
    cleaned = {k: v.strip() for k, v in values.items() if v and v.strip()}
    return _validate(Secrets, cleaned, p.name)
