"""피처 스토어: state/features/<date>.parquet, 키 (date, ticker), 쓰기는 멱등.

- 지표는 indicators.indicator_frame 그대로(워밍업 NaN 유지).
- 재무비율은 사업연도 Y 값을 Y+1 년 4월 1일부터 적용(룩어헤드 방지). US 비율은 Phase 7 (NaN).
- 투자자동향은 KIS 가 최근 30영업일만 주므로 백필엔 없고(NaN) 스냅샷부터 누적된다.
- 과거 asof 스냅샷은 금지(수집기는 현재 데이터만 준다).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from trader.broker.models import Candle, InvestorFlow, Market
from trader.config import ConfigError, Universe
from trader.platform import paths
from trader.sources import indicators
from trader.sources.dart import ANNUAL, basic_ratios

logger = logging.getLogger("trader.ml")

RATIO_COLUMNS = {"op_margin": "영업이익률", "roe": "ROE", "debt_ratio": "부채비율"}
FLOW_COLUMNS = ["flow_foreign_20d", "flow_inst_20d", "flow_indiv_20d"]
COUNT_COLUMNS = ["news_count", "community_count"]
FEATURE_COLUMNS: list[str] = (
    ["date", "ticker", "market", "close", "volume"]
    + indicators.COLUMNS
    + FLOW_COLUMNS
    + list(RATIO_COLUMNS)
    + COUNT_COLUMNS
)
MAX_DAYS = 1000  # KIS 일봉 10페이지 상한
MAX_PAST = timedelta(hours=24)


def _features_dir(state: Path | None) -> Path:
    base = state if state is not None else paths.state_dir()
    d = base / "features"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flow_sums(flows: list[InvestorFlow] | None, days: int = 20) -> dict[str, int | None]:
    if not flows:
        return dict.fromkeys(FLOW_COLUMNS)
    recent = flows[-days:]
    return {
        "flow_foreign_20d": sum(f.foreign_net for f in recent),
        "flow_inst_20d": sum(f.institution_net for f in recent),
        "flow_indiv_20d": sum(f.individual_net for f in recent),
    }


def _ratio_cols(ratios: dict | None) -> dict[str, float | None]:
    return {col: (ratios or {}).get(key) for col, key in RATIO_COLUMNS.items()}


def snapshot_row(
    *,
    ticker: str,
    market: Market,
    candles: list[Candle],
    flows: list[InvestorFlow] | None,
    ratios: dict | None,
    news_count: int | None,
    community_count: int | None,
) -> dict[str, Any]:
    """한 종목의 오늘 행. date 는 마지막 일봉 날짜."""
    row: dict[str, Any] = {
        "date": candles[-1].date,
        "ticker": ticker,
        "market": market,
        "close": float(candles[-1].close),
        "volume": int(candles[-1].volume),
    }
    row.update(indicators.latest(candles))
    row.update(_flow_sums(flows))
    row.update(_ratio_cols(ratios))
    row["news_count"] = news_count
    row["community_count"] = community_count
    return row


def _effective_fy(d: date) -> int:
    """d 시점에 적용되는 사업연도 (4월 1일부터 전년도)."""
    return d.year - 1 if d.month >= 4 else d.year - 2


def history_rows(
    *,
    ticker: str,
    market: Market,
    candles: list[Candle],
    ratios_by_year: dict[int, dict] | None,
) -> pd.DataFrame:
    """일봉 전 구간의 행들. 비율은 해당 날짜의 사업연도 값, 수급·건수는 NaN."""
    df = indicators.indicator_frame(indicators.to_frame(candles)).reset_index()
    out = pd.DataFrame({"date": df["date"], "ticker": ticker, "market": market})
    out["close"] = df["close"]
    out["volume"] = df["volume"].astype("int64")
    for c in indicators.COLUMNS:
        out[c] = df[c]
    for c in FLOW_COLUMNS:
        out[c] = pd.array([None] * len(out), dtype="Float64")
    years = out["date"].map(_effective_fy)
    for col, key in RATIO_COLUMNS.items():
        out[col] = pd.array(
            [((ratios_by_year or {}).get(int(y)) or {}).get(key) for y in years], dtype="Float64"
        )
    for c in COUNT_COLUMNS:
        out[c] = pd.array([None] * len(out), dtype="Float64")
    return out[FEATURE_COLUMNS]


def write_rows(df: pd.DataFrame, state: Path | None = None) -> list[Path]:
    """날짜별 parquet 에 (date, ticker) 기준으로 덮어쓴다. 멱등."""
    d = _features_dir(state)
    written: list[Path] = []
    for day, part in df.groupby("date", sort=True):
        path = d / f"{pd.Timestamp(day).date().isoformat()}.parquet"
        part = part.copy()
        part["date"] = part["date"].map(lambda x: pd.Timestamp(x).date())
        if path.exists():
            old = pd.read_parquet(path)
            old = old[~old["ticker"].isin(part["ticker"])]
            part = pd.concat([old, part], ignore_index=True)
        tmp = path.with_suffix(".parquet.tmp")
        part.reset_index(drop=True).to_parquet(tmp, index=False)
        tmp.replace(path)
        written.append(path)
    return written


def _ratios_by_year_kr(dart: Any, ticker: str, years: list[int]) -> dict[int, dict]:
    """연도별 비율. DART 조회 실패는 빈 dict (가격 지표 행은 그대로 저장한다)."""
    try:
        code = dart.corp_code(ticker)
    except Exception as e:  # noqa: BLE001 - DART 점검·한도 초과 등
        logger.warning("DART 고유번호 조회 실패 %s: %s", ticker, type(e).__name__)
        return {}
    if not code:
        return {}
    out: dict[int, dict] = {}
    for y in years:
        try:
            fin = dart.financials(code, y, ANNUAL)
        except Exception as e:  # noqa: BLE001 - 연도 하나 실패는 건너뛴다
            logger.warning("DART 재무 %s %s 실패: %s", ticker, y, type(e).__name__)
            continue
        if fin:
            out[y] = basic_ratios(fin, shares=None, price=None)
    return out


def _targets(universe: Universe, markets: list[Market]) -> list[tuple[str, Market, str | None]]:
    targets: list[tuple[str, Market, str | None]] = []
    if "KR" in markets:
        targets += [(t, "KR", None) for t in universe.kr if t not in universe.exclude]
    if "US" in markets:
        targets += [
            (s.symbol, "US", s.exchange) for s in universe.us if s.symbol not in universe.exclude
        ]
    return targets


def backfill(
    *,
    market_client: Any,
    dart: Any | None,
    universe: Universe,
    markets: list[Market],
    years: int = 3,
    state: Path | None = None,
    progress: Callable[[str], None] = lambda s: None,
) -> int:
    """유니버스 전 종목의 과거 피처를 재구성해 저장한다. 종목 실패는 로그 후 계속. 반환은 행 수."""
    days = min(years * 252, MAX_DAYS)
    if years * 252 > MAX_DAYS:
        logger.warning("일봉 상한 %d봉으로 제한합니다 (요청 %d년)", MAX_DAYS, years)
    total = 0
    for ticker, market, exchange in _targets(universe, markets):
        try:
            candles = (
                market_client.get_daily_kr(ticker, days=days)
                if market == "KR"
                else market_client.get_daily_us(ticker, exchange, days=days)
            )
            if not candles:
                raise ValueError("일봉 없음")
            fy_years = sorted({_effective_fy(c.date) for c in candles})
            ratios = _ratios_by_year_kr(dart, ticker, fy_years) if market == "KR" and dart else None
            df = history_rows(ticker=ticker, market=market, candles=candles, ratios_by_year=ratios)
            write_rows(df, state)
            total += len(df)
            progress(f"{market} {ticker}: {len(df)}행")
        except Exception as e:  # noqa: BLE001 - 종목 단위 격리
            logger.warning("백필 실패 %s %s: %s: %s", market, ticker, type(e).__name__, e)
            progress(f"{market} {ticker}: 실패 ({type(e).__name__})")
    return total


def snapshot(
    *,
    market_client: Any,
    dart: Any | None,
    universe: Universe,
    markets: list[Market],
    asof: datetime | None = None,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str], None] = lambda s: None,
) -> int:
    """오늘 행(종목당 1행)을 저장한다. 뉴스·커뮤니티 건수는 Phase 7 에서 채운다."""
    asof = asof or now()
    if now() - asof > MAX_PAST:
        raise ConfigError(
            f"과거 asof 스냅샷은 Phase 7 백테스트에서 지원합니다 (asof={asof.isoformat()})"
        )
    rows: list[dict] = []
    for ticker, market, exchange in _targets(universe, markets):
        try:
            candles = (
                market_client.get_daily_kr(ticker, days=260)
                if market == "KR"
                else market_client.get_daily_us(ticker, exchange, days=260)
            )
            if not candles:
                raise ValueError("일봉 없음")
            flows = market_client.get_investor_kr(ticker, days=20) if market == "KR" else None
            ratios = None
            if market == "KR" and dart is not None:
                by_year = _ratios_by_year_kr(dart, ticker, [_effective_fy(candles[-1].date)])
                ratios = next(iter(by_year.values()), None)
            rows.append(
                snapshot_row(
                    ticker=ticker,
                    market=market,
                    candles=candles,
                    flows=flows,
                    ratios=ratios,
                    news_count=None,
                    community_count=None,
                )
            )
            progress(f"{market} {ticker}: ok")
        except Exception as e:  # noqa: BLE001
            logger.warning("스냅샷 실패 %s %s: %s", market, ticker, type(e).__name__)
            progress(f"{market} {ticker}: 실패 ({type(e).__name__})")
    if not rows:
        return 0
    df = pd.DataFrame(rows)[FEATURE_COLUMNS]
    write_rows(df, state)
    return len(df)
