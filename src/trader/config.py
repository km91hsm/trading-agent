"""설정 로더 5개: settings / accounts / sources / universe / secrets.

yaml 모델은 전부 extra="forbid" 라 yaml 키를 추가하면 모델도 같이 바꾼다.
검증·파일 오류는 모두 ConfigError 로 통일한다.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from trader.platform import paths

logger = logging.getLogger("trader.config")

Env = Literal["paper", "real"]
Mode = Literal["auto", "approve", "off"]
Market = Literal["KR", "US"]
# 시장 현지 시간대 (주말·현지 날짜 판정, 분봉 시각, 체결 조회 날짜에 공통으로 쓴다)
KST = ZoneInfo("Asia/Seoul")  # 운영 기준 시간대. 모듈마다 ZoneInfo 를 다시 만들지 않는다
MARKET_TZ: dict[str, ZoneInfo] = {"KR": KST, "US": ZoneInfo("America/New_York")}


class ConfigError(Exception):
    """설정 파일 읽기·검증 실패."""


# --- settings.yaml ---------------------------------------------------------


class HorizonProfile(BaseModel):
    """보유기간 프로필(Phase 23 T): 결정의 `horizon_days` 가 정하는 장중 점검 주기·임계.
    단기 결정은 자주·작은 변동에, 장기 결정은 큰 변동·뉴스 묶음에만 다시 본다."""

    model_config = ConfigDict(extra="forbid")

    move_pct: float = Field(gt=0, le=0.5)  # 급등락 재평가 트리거(평단 대비)
    reeval_interval_min: int = Field(ge=1)  # 같은 종목 재평가 세션 최소 간격(분)
    periodic_check_min: int = Field(ge=0)  # 시간 단위 정기 재평가 간격(분), 0 = 없음
    news_min_novel: int = Field(ge=1)  # 뉴스 재분석에 필요한 새 뉴스 건수
    daily_reanalyze_every: int = Field(ge=1)  # 보유 종목 아침 재분석 주기(운영일)


def _default_profiles() -> dict[str, HorizonProfile]:
    return {
        "short": HorizonProfile(
            move_pct=0.04,
            reeval_interval_min=15,
            periodic_check_min=60,
            news_min_novel=1,
            daily_reanalyze_every=1,
        ),
        "mid": HorizonProfile(
            move_pct=0.07,
            reeval_interval_min=30,
            periodic_check_min=0,
            news_min_novel=1,
            daily_reanalyze_every=2,
        ),
        "long": HorizonProfile(
            move_pct=0.10,
            reeval_interval_min=60,
            periodic_check_min=0,
            news_min_novel=3,
            daily_reanalyze_every=5,
        ),
    }


class ScalpCfg(BaseModel):
    """분 단위 단타 층(Phase 23 S). **모의 계좌 전용·기본 끔.** 1분봉 신호는 파이썬 순수 함수
    (`scalp/signals.py`)이고 에이전트는 후보(picks·뉴스·watch)와 허용 여부(strategist `scalp_ok`)만
    정한다. 실전 계좌는 설정이 켜져 있어도 기록만 한다."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_weight_pct: float = Field(default=0.05, gt=0, le=0.25)  # 계좌 NAV 대비 진입 비중
    max_positions: int = Field(default=2, ge=1, le=10)  # 동시 보유 단타 종목 수
    max_universe: int = Field(default=10, ge=1, le=30)  # 틱마다 분봉을 받는 종목 수 상한
    volume_mult: float = Field(default=3.0, gt=1)  # 최근 5분 거래량 ≥ 직전 20분 평균 × 배수
    take_profit_pct: float = Field(default=0.015, gt=0, le=0.2)
    stop_pct: float = Field(default=0.01, gt=0, le=0.2)
    max_hold_min: int = Field(default=60, ge=5, le=390)
    warmup_min: int = Field(default=15, ge=1, le=120)  # 개장 뒤 이 시간은 진입 없음
    close_before_min: int = Field(
        default=20, ge=1, le=120
    )  # 마감 전 이 시간부터 신규 금지·전량 청산
    daily_stop_pct: float = Field(
        default=0.01, gt=0, le=0.2
    )  # 단타 일일 실현손실(NAV 비중 합) 한도


class RepriceCfg(BaseModel):
    """아침 미체결 재호가(Phase 23 B): 개장 뒤 after_min 분에 운영일 1회, 오늘 낸 지정가 미체결
    주문을 취소하고 같은 결정으로 현재가 지정가 재제출(게이트 재통과). 원 지정가 대비
    max_chase_pct 넘게 불리하면 재제출하지 않고 알린다. dry_run·off 계좌는 제외."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    after_min: int = Field(default=30, ge=1, le=300)
    max_chase_pct: float = Field(default=0.01, gt=0, le=0.1)


class RebalanceCfg(BaseModel):
    """주간 리밸런스(Phase 25 단계 7). 최근 LLM 결정의 target_weight 대비 ±band_pct 넘게 벗어난
    보유만 TRIM 먼저·ADD 나중, 결정 행 없이(decision_id=None) 게이트를 그대로 통과한 수량만."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    band_pct: float = Field(default=0.05, gt=0, le=0.5)  # 비중 차이(소수, 0.05 = 5%p)
    lookback_days: int = Field(default=30, ge=1, le=365)  # 목표로 쓸 결정의 최대 나이


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: float = 0.25
    max_positions: int = 5
    daily_loss_stop_pct: float = -0.05
    stop_loss_pct: float = -0.15
    min_cash_pct: float = 0.0
    max_sector_pct: float = Field(default=0.40, gt=0, le=1)  # 같은 섹터 합계 상한 (BUY/ADD 뒤)
    # 보유기간 프로필(Phase 23 T): short(≤10일)·mid(11~40)·long(>40) 셋이 전부 있어야 한다
    horizon_profiles: dict[str, HorizonProfile] = Field(default_factory=_default_profiles)
    reprice: RepriceCfg = Field(default_factory=RepriceCfg)  # 아침 미체결 재호가(Phase 23 B)

    @field_validator("horizon_profiles")
    @classmethod
    def _profiles_complete(cls, v: dict[str, HorizonProfile]) -> dict[str, HorizonProfile]:
        missing = {"short", "mid", "long"} - set(v)
        if missing:
            raise ValueError(f"horizon_profiles 에 빠진 프로필: {sorted(missing)}")
        return v


class HedgeInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange: Literal["NAS", "NYS", "AMS"] | None = None  # US 만


def _coerce_instrument(v: Any) -> Any:
    return {"symbol": v} if isinstance(v, str) else v


class HedgeCfg(BaseModel):
    """하락장 대응 (Phase 17 H): 국면 판정 3지표 + 인버스 ETF 헤지 슬리브. 전부 파이썬 규칙.

    국면: ① 지수 proxy 종가 < 200일 SMA, ② 20거래일 수익률 ≤ momentum_min, ③ 변동성(US 는 FRED VIX ≥
    vix_min, KR 은 proxy 20일 실현변동성 연율 ≥ realized_vol_min). count ≥ bear_min_signals → bear,
    0 → bull, 그 외 neutral. bear 해제는 count ≤ 1 이 exit_streak_days 연속일 때(채찍질 방지)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_weight_pct: float = Field(default=0.20, ge=0, le=0.5)
    min_weight_pct: float = Field(default=0.05, ge=0, le=0.5)  # 시장 세션 제안의 하한 (Phase 18)
    market_session: bool = True  # 매일 시장 세션(거시 애널리스트·토론·전략가) 실행 여부 (Phase 18)
    leverage: bool = True  # 3지표 모두 켜지면 −2x, 아니면 −1x
    bear_min_signals: int = Field(default=2, ge=1, le=3)
    exit_streak_days: int = Field(default=5, ge=1, le=30)
    # Phase 19: 전략가(시장 세션) RISK_OFF 를 4번째 국면 지표로 센다(지표 0개면 bear 진입 불가),
    # RISK_ON 은 지표가 bear_min_signals 와 같을 때 하루 진입 보류. 둘 다 confidence 기준 이상만
    agent_signal: bool = True
    agent_min_confidence: float = Field(default=0.7, ge=0, le=1)
    # Phase 22: 전략가 제안(확신 ≥ agent_min_confidence)이 국면 한도를 한 칸 옮긴다 —
    # neutral+RISK_ON 20%·4, neutral+RISK_OFF 10%·2, bull+RISK_OFF → neutral 한도, bear+RISK_ON →
    # 신규 매수를 10%·2 한도로 조건부 허용. 헤지·손절은 무관. 끄면 Phase 17 국면 한도 그대로
    agent_gate: bool = True
    # Phase 21: KR 외인 수급(지수 proxy 20일·5일 순매수 합 모두 음수) 을 보조 지표로 센다. 기본 끔
    flow_signal: bool = False
    # Phase 21: bear 유지 중 보유 상품(−1x/−2x)이 판정과 다른 날이 switch_streak_days 연속이면 전량
    # 매도 뒤 다음 운영일 재진입(같은 날 둘 다 내지 않음 — 매도 현금을 확인할 수 없다). 기본 끔
    switch_instruments: bool = False
    switch_streak_days: int = Field(default=3, ge=1, le=30)
    ma_days: int = Field(default=200, ge=20, le=400)
    momentum_days: int = Field(default=20, ge=5, le=120)
    momentum_min: float = -0.05
    vix_min: float = 25.0
    realized_vol_min: float = 0.25
    instruments: dict[Market, dict[Literal["inverse", "inverse2x"], HedgeInstrument]] = Field(
        default_factory=lambda: {
            "KR": {
                "inverse": HedgeInstrument(symbol="114800"),
                "inverse2x": HedgeInstrument(symbol="252670"),
            },
            "US": {
                "inverse": HedgeInstrument(symbol="SH", exchange="AMS"),
                "inverse2x": HedgeInstrument(symbol="SDS", exchange="AMS"),
            },
        }
    )
    index_proxy: dict[Market, HedgeInstrument] = Field(
        default_factory=lambda: {
            "KR": HedgeInstrument(symbol="069500"),
            "US": HedgeInstrument(symbol="SPY", exchange="AMS"),
        }
    )

    @model_validator(mode="after")
    def _weights(self) -> HedgeCfg:
        if self.min_weight_pct > self.max_weight_pct:
            raise ValueError("hedge.min_weight_pct 는 max_weight_pct 이하여야 합니다")
        return self

    @field_validator("instruments", mode="before")
    @classmethod
    def _coerce_instruments(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        return {m: {k: _coerce_instrument(x) for k, x in (d or {}).items()} for m, d in v.items()}

    @field_validator("index_proxy", mode="before")
    @classmethod
    def _coerce_proxy(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        return {m: _coerce_instrument(x) for m, x in v.items()}

    def tickers(self, market: str) -> set[str]:
        """그 시장의 헤지 종목 코드 (monitor·종목 수·섹터 계산·스캔에서 제외)."""
        return {i.symbol for i in self.instruments.get(market, {}).values()}  # type: ignore[arg-type]

    def instrument(self, market: str, *, leveraged: bool) -> HedgeInstrument | None:
        d = self.instruments.get(market, {})  # type: ignore[arg-type]
        if leveraged and self.leverage and "inverse2x" in d:
            return d["inverse2x"]
        return d.get("inverse")


class UsageThrottle(BaseModel):
    """구독 사용률 감속 (Phase 24). 마지막 관측(세션 rate_limit_event·상태줄 파일)의 5시간·7일 창 중
    하나라도 soft 이상이면 보유 재평가(resume)만, hard 이상이면 새 세션을 전부 막는다.
    관측이 없거나 max_age_h 보다 오래면 막지 않는다(고정 상한 daily_session_cap 만)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    soft_pct: float = Field(0.80, gt=0, le=1)
    hard_pct: float = Field(0.95, gt=0, le=1)
    max_age_h: float = Field(6.0, gt=0)


class LlmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = 5.0
    timeout_s: int = 600
    # 종목 analyze + 시장 세션 + 장중 발굴·재분석 합계. Phase 24 에 40→80
    # (구독 사용률 감속 usage_throttle 이 실제 브레이크, 2~4주 통계 뒤 다시 정한다)
    daily_session_cap: int = 80
    usage_throttle: UsageThrottle = Field(default_factory=UsageThrottle)
    market_session_cap: int = 2  # 하루 시장 세션(kind=market) 상한 (종목 분석 예산 보호)
    # 대화형 Claude Code 상태줄이 남기는 구독 사용률 관측 파일 (usage.py). 비우면 표시 안 함
    usage_file: str | None = "~/.claude/rate_limits.json"


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    port: int = 8787


class HalfLives(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: int = 14
    mid: int = 90
    long: int = 365


class SimilarityCfg(BaseModel):
    """유사 뉴스 판정(Phase 23, `news_store.is_similar`): 토큰 Jaccard 또는 문자열 유사도."""

    model_config = ConfigDict(extra="forbid")

    jaccard: float = Field(default=0.6, ge=0, le=1)
    ratio: float = Field(default=0.8, ge=0, le=1)
    lookback_days: int = Field(default=3, ge=1, le=30)


class IntradayDiscoveryCfg(BaseModel):
    """장중 뉴스 발굴(Phase 23): 뉴스 수집(30분)마다 **새 내용** 뉴스가 있으면 같은 종목이라도
    재분석(오늘 결정이 있으면 재평가)하고 유니버스 밖은 scout 세션(kind=discover)으로 발굴한다.
    유사 기사만 들어오면 세션을 띄우지 않는다. 장중 창 안에서만."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_items: int = Field(default=1, ge=1, le=20)  # 종목 재분석에 필요한 새 뉴스 건수
    discover_cap: int = Field(default=20, ge=0, le=100)  # 시장별 하루 discover 세션 상한
    similarity: SimilarityCfg = Field(default_factory=SimilarityCfg)


class DiscoveryCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intraday: IntradayDiscoveryCfg = Field(default_factory=IntradayDiscoveryCfg)


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
    hedge: HedgeCfg = Field(default_factory=HedgeCfg)
    models: dict[str, str] = Field(default_factory=dict)
    debate_rounds: int = Field(default=1, ge=0, le=2)
    anonymize: bool = False
    claude_bin: str | None = None
    llm: LlmSettings = Field(default_factory=LlmSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)  # Phase 23 장중 뉴스 발굴
    scalp: ScalpCfg = Field(default_factory=ScalpCfg)
    rebalance: RebalanceCfg = Field(
        default_factory=RebalanceCfg
    )  # Phase 25 주간 리밸런스  # Phase 23 분 단위 단타(모의 전용, 기본 끔)


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


DEFAULT_MARKET_QUERIES: dict[str, list[str]] = {
    "KR": [
        "증시",
        "코스피 OR 코스닥",
        "실적 발표",
        "수주 OR 계약",
        "신약 OR 임상",
        "반도체 OR 2차전지 OR 자동차",
        "금리 OR 환율",
    ],
    "US": [
        "stock market",
        "earnings",
        "Fed OR Treasury yields",
        "guidance OR upgrade",
        "semiconductor OR AI OR biotech",
    ],
}


class NewsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    google_rss: bool = True
    naver: bool = True
    max_items: int = 20
    macro_items: int = 10
    # 시장 전체 뉴스 저장소(Phase 19): 데몬이 질의 묶음을 돌려 원장 news_items 에 쌓는다
    market_queries: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_MARKET_QUERIES.items()}
    )
    store_retention_days: int = Field(default=14, ge=1, le=365)
    store_hours: int = Field(default=48, ge=1, le=24 * 14)  # 세션·발굴 입력의 최근 창
    store_items: int = Field(default=60, ge=1, le=500)  # 발굴 입력 건수 상한
    # 세이브티커 로컬 수집 폴더(사용자 별도 수집기). 상대 경로는 프로젝트 루트 기준, 없으면 건너뜀
    saveticker_dir: str | None = "state/external/saveticker"


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
    naver_blog: list[str] = Field(default_factory=list)  # 블로그 id (rss.blog.naver.com/<id>.xml)
    naver_blog_posts: int = Field(default=5, ge=1, le=20)  # 키워드 무관 최근 글 수
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


class SectorMap(BaseModel):
    """config/sectors.yaml — 종목 → 동종업 대분류 (사람이 관리하는 결정적 데이터, 스펙 §3).

    피처 스토어 `*_vs_sector`(비율 − 같은 날·시장·섹터 중앙값)와 `00_inputs.fundamental.peer`
    가 쓴다."""

    model_config = ConfigDict(extra="forbid")

    sectors: list[str] = Field(default_factory=list)
    kr: dict[str, str] = Field(default_factory=dict)
    us: dict[str, str] = Field(default_factory=dict)
    # KIS 업종 → 대분류 (KR 은 현재가 응답의 업종한글명, US 는 현재가상세 e_icod — 한글 업종명).
    # `trader universe sectors` 가 빠진 종목의 섹터를 제안할 때 쓴다. 표에 없으면 추측하지 않음.
    kr_industry_to_sector: dict[str, str] = Field(default_factory=dict)
    us_industry_to_sector: dict[str, str] = Field(default_factory=dict)

    @field_validator("kr", "us", "kr_industry_to_sector", "us_industry_to_sector")
    @classmethod
    def _declared(cls, v: dict[str, str], info) -> dict[str, str]:
        declared = set(info.data.get("sectors") or [])
        bad = sorted({s for s in v.values() if s not in declared})
        if bad:
            raise ValueError(f"sectors 에 없는 대분류: {bad}")
        return v

    def sector_of(self, ticker: str, market: Market) -> str | None:
        table = self.kr if market == "KR" else self.us
        return table.get(ticker)

    def classify(self, industry: str | None, market: Market) -> str | None:
        if not industry:
            return None
        table = self.kr_industry_to_sector if market == "KR" else self.us_industry_to_sector
        return table.get(str(industry).strip())


# --- secrets/.env ----------------------------------------------------------


class Secrets(BaseModel):
    """필드명 = 환경변수명. 비어 있지 않은 환경변수가 .env 값을 덮어쓴다."""

    model_config = ConfigDict(extra="ignore")

    KIS_PAPER_APP_KEY: str | None = None
    KIS_PAPER_APP_SECRET: str | None = None
    KIS_REAL_APP_KEY: str | None = None
    KIS_REAL_APP_SECRET: str | None = None
    # 계좌번호(8자리). accounts.yaml 의 cano 가 자리표시자("00000000"·빈 값)면 env 별로
    # 여기서 읽는다 (저장소를 공개해도 커밋에 계좌번호가 남지 않도록, 2026-10-25)
    KIS_PAPER_CANO: str | None = None
    KIS_REAL_CANO: str | None = None
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
    analyze: str | None = None  # 호환용. scan 뒤 바로 analyze 가 이어지므로 쓰지 않는다
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

    @field_validator("scan")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_hm(v)

    def scan_hm(self) -> tuple[int, int]:
        return _hm(self.scan)


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


class IntervalJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_s: int = Field(default=1800, ge=60, le=86400)


class ReconcileSchedule(BaseModel):
    """야간 KIS 대조 시각(시장별 현지 시각, 그 시장 `tz`). 체결 동기화(16:10 KST·07:10 KST) 뒤."""

    model_config = ConfigDict(extra="forbid")

    kr: str = "16:40"
    us: str = "16:40"

    @field_validator("kr", "us")
    @classmethod
    def _hm_ok(cls, v: str) -> str:
        return _check_hm(v)


class RebalanceSchedule(BaseModel):
    """주간 리밸런스 요일·시장별 현지 시각(마감 전, 지정가가 체결될 시간을 둔다)."""

    model_config = ConfigDict(extra="forbid")

    weekday: str = "fri"
    kr: str = "14:30"
    us: str = "15:00"

    @field_validator("weekday")
    @classmethod
    def _w(cls, v: str) -> str:
        if v not in WEEKDAYS:
            raise ValueError(f"요일은 {WEEKDAYS} 중 하나여야 합니다: {v!r}")
        return v

    @field_validator("kr", "us")
    @classmethod
    def _hm_ok(cls, v: str) -> str:
        return _check_hm(v)


class ScheduleConfig(BaseModel):
    """config/schedule.yaml (KST). 데몬이 잡을 등록할 때 읽는다."""

    model_config = ConfigDict(extra="forbid")

    kr: MarketSchedule
    us: MarketSchedule
    reflect: str
    ml_train: WeeklyJob
    news_collect: IntervalJob = Field(default_factory=IntervalJob)  # 시장 전체 뉴스 수집 주기
    reconcile: ReconcileSchedule = Field(
        default_factory=ReconcileSchedule
    )  # 야간 KIS 대조(Phase 21)
    rebalance: RebalanceSchedule = Field(default_factory=RebalanceSchedule)  # Phase 25

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
    # absolute: fwd_return 을 ±thresholds 로 3분류. relative_tercile: 같은 날·시장 안 순위 삼분위
    # (기준선 1/3 고정, 시장 등락이 라벨에서 빠짐 — scan 랭커 용도와 맞는다)
    mode: Literal["absolute", "relative_tercile"] = "absolute"


class TrainCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_years: int = Field(default=3, ge=1, le=10)
    walk_forward_folds: int = Field(default=6, ge=2, le=20)


class QuantModelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    promote_min_accuracy: float = 0.40
    promote_min_alpha: float = 0.0
    # accuracy: 합산 정확도 > 기준선 ∧ ≥ promote_min_accuracy ∧ 알파 > promote_min_alpha (기존).
    # alpha: scan 랭커 용도 — 평균 알파(상위 N 초과수익) ≥ promote_min_alpha ∧ 알파가 양인 폴드
    #        비율 ≥ promote_min_positive_folds. 정확도는 보지 않는다 (상대 라벨 기준선은 1/3 고정).
    promote_metric: Literal["accuracy", "alpha"] = "accuracy"
    promote_min_positive_folds: float = Field(default=0.67, ge=0.0, le=1.0)
    promote_top_n: int = Field(default=5, ge=1, le=50)  # alpha_topn 의 N (scan 랭커 상위 N 과 같게)
    backend: Literal["auto", "sklearn", "lightgbm"] = "sklearn"  # 플랫폼 간 동일 동작을 위해 고정
    min_up_predictions: int = 5  # 폴드마다 up 예측 최소 건수 (알파 추정 노이즈 방지)
    max_iter: int = Field(default=200, ge=1, le=5000)  # 부스팅 반복(트리 수). 테스트는 작게
    # `ml tune` 이 탐색하는 두 값 (sklearn max_leaf_nodes / lightgbm num_leaves). 자동 적용 없음.
    learning_rate: float = Field(default=0.05, gt=0, le=1.0)
    num_leaves: int = Field(default=15, ge=2, le=255)
    # 알파 기준은 왕복비용 기준이라 시장마다 다를 수 있다 (KR 30bp, US 50bp).
    # 없는 시장은 promote_min_alpha.
    promote_min_alpha_by_market: dict[Literal["KR", "US"], float] = Field(default_factory=dict)

    def min_alpha_for(self, market: str | None) -> float:
        if market is not None and market in self.promote_min_alpha_by_market:
            return self.promote_min_alpha_by_market[market]
        return self.promote_min_alpha


class RankerCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class CalibrationCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_decisions: int = 60


class RlSizerCfg(BaseModel):
    """RL 사이저 (스펙 §9.4, 실험). 모의 계좌 한정, 승격은 `trader rl promote` 수동."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    seeds: int = Field(default=3, ge=1, le=10)
    mode: Literal["shadow", "apply"] = "shadow"  # apply 도 모의 계좌에만
    backend: Literal["auto", "cem", "ppo"] = "auto"  # ppo 는 `uv sync --extra rl`
    max_weight: float = Field(default=0.25, gt=0, le=0.25)
    drawdown_penalty: float = Field(default=1.0, ge=0)
    cost_bp: int = Field(default=30, ge=0)
    total_timesteps: int = Field(default=20000, ge=1)
    cem_iterations: int = Field(default=30, ge=1)
    cem_population: int = Field(default=64, ge=4)
    min_memory_steps: int = Field(default=200, ge=1)
    min_steps: int = Field(default=300, ge=1)
    fixed_weight: float = Field(default=0.10, ge=0, le=0.25)
    horizon_days: int = Field(default=5, ge=1, le=60)  # 보상 정규화용, train 이 label 값으로 덮음


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


CANO_PLACEHOLDER = "00000000"


def _cano_from_secrets(a: AccountConfig, secrets: Secrets | None) -> AccountConfig:
    """cano 가 자리표시자면 `KIS_<PAPER|REAL>_CANO` 로 채운다(없으면 그대로 두고 경고)."""
    if a.cano and a.cano != CANO_PLACEHOLDER and not a.cano.startswith("${"):
        return a
    key = "KIS_PAPER_CANO" if a.env == "paper" else "KIS_REAL_CANO"
    value = (getattr(secrets, key, None) or "").strip() if secrets is not None else ""
    if not value:
        logger.warning("계좌 %s 의 계좌번호가 자리표시자이고 secrets %s 도 비어 있음", a.id, key)
        return a
    return a.model_copy(update={"cano": value})


def load_accounts(
    path: Path | None = None, *, secrets: Secrets | None = None
) -> list[AccountConfig]:
    """accounts.yaml → 계좌 목록. cano 자리표시자는 secrets(.env) 의 `KIS_*_CANO` 로 채운다
    (secrets 를 넘기지 않으면 자리표시자가 있을 때만 `load_secrets()` 를 읽는다)."""
    p = path or paths.config_dir() / "accounts.yaml"
    accounts = _validate(_AccountsFile, _read_yaml(p), p.name).accounts
    if any(not a.cano or a.cano == CANO_PLACEHOLDER or a.cano.startswith("${") for a in accounts):
        if secrets is None:
            secrets = load_secrets()
        accounts = [_cano_from_secrets(a, secrets) for a in accounts]
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


def load_sectors(path: Path | None = None) -> SectorMap:
    """파일이 없으면 빈 맵 (파생 열은 전부 NaN, peer 절은 건너뜀)."""
    p = path or paths.config_dir() / "sectors.yaml"
    return _validate(SectorMap, _read_yaml(p), p.name)


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
