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

import numpy as np
import pandas as pd

from trader.broker.models import Candle, InvestorFlow, Market
from trader.config import ConfigError, SectorMap, Universe, load_sectors
from trader.platform import paths
from trader.sources import indicators
from trader.sources.dart import ANNUAL, basic_ratios

logger = logging.getLogger("trader.ml")

RATIO_COLUMNS = {"op_margin": "영업이익률", "roe": "ROE", "debt_ratio": "부채비율"}
FLOW_COLUMNS = ["flow_foreign_20d", "flow_inst_20d", "flow_indiv_20d"]
COUNT_COLUMNS = ["news_count", "community_count"]
# 동종업 상대 비율: 비율 − 같은 날·같은 시장·같은 섹터(config/sectors.yaml) 중앙값.
# write_rows 가 날짜 파일 전체로 다시 계산하는 파생 열이라 백필 묶음 쓰기 뒤에도 값이 같다.
SECTOR_COLUMNS = [f"{c}_vs_sector" for c in RATIO_COLUMNS]
MIN_SECTOR_PEERS = 3
KEY_COLUMNS = ["date", "ticker", "market", "close", "volume"]
FEATURE_COLUMNS: list[str] = (
    KEY_COLUMNS
    + indicators.COLUMNS
    + FLOW_COLUMNS
    + list(RATIO_COLUMNS)
    + COUNT_COLUMNS
    + SECTOR_COLUMNS
)
MAX_DAYS = 1000  # KIS 일봉 10페이지 상한

# 고정 스키마. snapshot/backfill 어느 경로로 써도 write_rows 가 이 dtype 으로 맞춘다.
FEATURE_DTYPES: dict[str, str] = {
    "ticker": "string",
    "market": "string",
    "close": "float64",
    "volume": "int64",
    **{
        c: "Float64"
        for c in indicators.COLUMNS
        + FLOW_COLUMNS
        + list(RATIO_COLUMNS)
        + COUNT_COLUMNS
        + SECTOR_COLUMNS
    },
}


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    # 없는 열(파생 열을 아직 안 계산한 행·옛 파일)은 NaN 으로 채운다
    out = df.reindex(columns=FEATURE_COLUMNS).copy()
    out["date"] = out["date"].map(lambda x: pd.Timestamp(x).date())
    for col, dtype in FEATURE_DTYPES.items():
        out[col] = out[col].astype(dtype)
    return out


MAX_PAST = timedelta(hours=24)


def zscore(s: pd.Series, *, fill: float | None = None) -> pd.Series:
    """열별 z-score (ddof=0). 표준편차가 0·비유한이면 전부 0. 숫자가 아닌 값은 NaN 으로 바꾸고
    fill 이 있으면 결측을 그 값으로 채운다 (정량 점수는 0, ML 랭커는 NaN 유지)."""
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    z = (s - s.mean()) / sd
    return z.fillna(fill) if fill is not None else z
BACKFILL_CHUNK = 10  # 백필 때 날짜 파일을 다시 쓰는 단위(종목 수)


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
    """한 종목의 오늘 행. date 는 마지막 **완성된** 일봉 날짜(거래량 0 스텁은 버린다)."""
    candles = drop_incomplete_tail(candles)
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
    row.update(dict.fromkeys(SECTOR_COLUMNS))  # write_rows 가 채운다
    missing = [c for c in FEATURE_COLUMNS if c not in row]
    if missing:  # _coerce 가 reindex 라 여기서 막지 않으면 조용히 NaN 으로 저장된다
        raise ValueError(f"스냅샷 행에 피처 열이 없습니다: {missing}")
    return row


def _effective_fy(d: date) -> int:
    """d 시점에 적용되는 사업연도 (4월 1일부터 전년도)."""
    return d.year - 1 if d.month >= 4 else d.year - 2


def drop_incomplete_tail(candles: list[Candle]) -> list[Candle]:
    """마지막 봉이 거래량 0 이면 버린다. KIS 일봉은 개장 전·장중 초반에 오늘 봉을 거래량 0 스텁으로
    돌려주는데, 그 행이 스토어에 들어가면 거래대금 0 으로 scan 이 전 종목을 거른다(실기 10-20)."""
    out = list(candles)
    while out and int(out[-1].volume) <= 0:
        out.pop()
    return out


def history_rows(
    *,
    ticker: str,
    market: Market,
    candles: list[Candle],
    ratios_by_year: dict[int, dict] | None,
) -> pd.DataFrame:
    """일봉 전 구간의 행들. 비율은 해당 날짜의 사업연도 값, 수급·건수는 NaN."""
    candles = drop_incomplete_tail(candles)
    if not candles:
        raise ValueError("완성된 일봉 없음 (전부 거래량 0)")
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
    for c in COUNT_COLUMNS + SECTOR_COLUMNS:
        out[c] = pd.array([None] * len(out), dtype="Float64")
    return out[FEATURE_COLUMNS]


def add_sector_relative(df: pd.DataFrame, sectors: SectorMap) -> pd.DataFrame:
    """`<비율>_vs_sector` = 비율 − 같은 (date, market, 섹터) 중앙값. 값이 있는 구성원이
    MIN_SECTOR_PEERS 미만이거나 섹터를 모르는 종목은 NaN (0 으로 두면 '평균 수준' 으로 읽힌다)."""
    out = df.copy()
    sector = pd.Series(
        [
            sectors.sector_of(str(t), m)
            for t, m in zip(out["ticker"], out["market"], strict=True)
        ],
        index=out.index,
        dtype="object",
    )
    keys = [pd.to_datetime(out["date"]), out["market"].astype(str), sector]
    for col in RATIO_COLUMNS:
        new = f"{col}_vs_sector"
        if col not in out.columns:
            out[new] = pd.array([None] * len(out), dtype="Float64")
            continue
        vals = pd.to_numeric(out[col], errors="coerce").astype("float64")
        g = vals.groupby(keys, dropna=True)
        med = g.transform("median")
        cnt = g.transform("count")
        rel = (vals - med).where(cnt >= MIN_SECTOR_PEERS)
        out[new] = pd.array(rel.to_numpy(), dtype="Float64")
    return out


def latest_rows(
    state: Path | None,
    market: str | None,
    *,
    tickers: list[str] | None = None,
    asof: date | None = None,
    max_files: int = 60,
) -> pd.DataFrame:
    """종목별 최신 행. 파일(`<date>.parquet`)을 최신부터 읽되 asof 이후 파일은 건너뛰고,
    tickers 가 전부 모이면(또는 max_files 에 닿으면) 멈춘다 — parquet 전부를 읽지 않는다."""
    fdir = _features_dir(state)
    files = sorted(fdir.glob("*.parquet"), reverse=True) if fdir.exists() else []
    if asof is not None:
        files = [f for f in files if f.stem <= asof.isoformat()]
    want = set(tickers) if tickers else None
    seen: set[str] = set()
    parts: list[pd.DataFrame] = []
    for f in files[:max_files]:
        # 이전 Phase 에 쓰인 파일은 뒤에 추가된 열이 없을 수 있다 → NaN 으로 맞춘다
        part = _coerce(pd.read_parquet(f).reindex(columns=list(FEATURE_COLUMNS)))
        if market:
            part = part[part["market"] == market]
        tick = part["ticker"].astype(str)
        mask = ~tick.isin(seen)
        if want is not None:
            mask &= tick.isin(want)
        part = part[mask].drop_duplicates(subset="ticker", keep="first")
        if not part.empty:
            parts.append(part)
            seen.update(part["ticker"].astype(str))
        if want is not None and want <= seen:
            break
    if not parts:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    out = pd.concat(parts, ignore_index=True)
    return _coerce(out.sort_values("ticker").reset_index(drop=True))


def _write_day_file(
    path: Path, part: pd.DataFrame, sectors: SectorMap, *, derive: bool = True
) -> None:
    part = _coerce(add_sector_relative(part, sectors) if derive else part)
    tmp = path.with_suffix(".parquet.tmp")
    part.reset_index(drop=True).to_parquet(tmp, index=False)
    tmp.replace(path)


def ticker_history(
    state: Path | None, market: str, ticker: str, *, days: int, max_files: int = 2000
) -> pd.DataFrame:
    """한 종목의 최근 days 행(날짜 오름차순). 최신 파일부터 거꾸로 읽어 모이면 멈춘다.

    웹 탐색용이라 스토어 전체를 읽지 않는다 (학습은 dataset.load_all)."""
    fdir = _features_dir(state)
    files = sorted(fdir.glob("*.parquet"), reverse=True) if fdir.exists() else []
    parts: list[pd.DataFrame] = []
    got = 0
    for f in files[:max_files]:
        raw = pd.read_parquet(f, filters=[("ticker", "==", ticker)])  # 행 필터 뒤에 변환
        if raw.empty:
            continue
        part = _coerce(raw.reindex(columns=list(FEATURE_COLUMNS)))
        part = part[(part["market"] == market) & (part["ticker"].astype(str) == ticker)]
        if not part.empty:
            parts.append(part)
            got += len(part)
        if got >= days:
            break
    if not parts:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    out = pd.concat(parts, ignore_index=True).sort_values("date").tail(days)
    return _coerce(out.reset_index(drop=True))


def write_rows(
    df: pd.DataFrame,
    state: Path | None = None,
    *,
    sectors: SectorMap | None = None,
    derive: bool = True,
) -> list[Path]:
    """날짜별 parquet 에 (date, ticker) 기준으로 덮어쓴다. 멱등. 파생 열(`*_vs_sector`)은 합친
    날짜 파일 전체로 다시 계산하므로 종목 묶음으로 나눠 써도 마지막 쓰기 뒤 값이 같다.
    derive=False 는 백필 묶음 쓰기용(끝에 `rederive(paths=)` 로 1회 계산), 기존 파생 값은 그대로."""
    d = _features_dir(state)
    sectors = sectors if sectors is not None else (load_sectors() if derive else SectorMap())
    written: list[Path] = []
    missing = [c for c in KEY_COLUMNS if c not in df.columns]
    if missing:
        # _coerce 는 reindex 라 빠진 열이 조용히 NaN 이 된다 → 식별 열 누락은 여기서, 피처 열 누락은
        # 생산자(snapshot_row·history_rows)가 막는다
        raise ValueError(f"피처 행에 열이 없습니다: {missing}")
    df = _coerce(df)
    for day, part in df.groupby("date", sort=True):
        path = d / f"{pd.Timestamp(day).date().isoformat()}.parquet"
        if path.exists():
            # 이전 Phase 파일은 열이 적을 수 있다 (latest_rows 와 같은 reindex)
            old = _coerce(pd.read_parquet(path).reindex(columns=FEATURE_COLUMNS))
            old = old[~old["ticker"].isin(part["ticker"])]
            part = pd.concat([old, part], ignore_index=True)
        _write_day_file(path, part, sectors, derive=derive)
        written.append(path)
    return written


def rederive(
    state: Path | None = None,
    *,
    sectors: SectorMap | None = None,
    progress: Callable[[str], None] = lambda s: None,
    paths: list[Path] | None = None,
) -> int:
    """날짜 파일의 파생 열(`*_vs_sector`)을 다시 계산해 쓴다 (KIS 호출 없음). 반환은 파일 수.
    paths 가 없으면 전체 — 섹터 맵을 바꾸거나 파생 열이 없는 옛 스토어를 올릴 때. 백필은 건드린
    파일만 넘긴다."""
    fdir = _features_dir(state)
    sectors = sectors if sectors is not None else load_sectors()
    files = sorted(paths) if paths is not None else sorted(fdir.glob("*.parquet"))
    for path in files:
        part = _coerce(pd.read_parquet(path).reindex(columns=FEATURE_COLUMNS))
        _write_day_file(path, part, sectors)
        progress(f"{path.stem}: {len(part)}행")
    return len(files)


def sector_medians(
    state: Path | None,
    market: str,
    sector: str,
    *,
    tickers: list[str],
    asof: date | None = None,
) -> dict[str, Any]:
    """같은 섹터 구성원(tickers)의 최신 행 비율 중앙값.

    반환 {sector, n, median{op_margin, roe, debt_ratio}} — n 은 비율이 하나라도 있는 구성원 수,
    중앙값은 열마다 값 있는 구성원으로."""
    rows = latest_rows(state, market, tickers=tickers, asof=asof) if tickers else None
    median: dict[str, float | None] = {}
    n = 0
    dates: dict[str, str] | None = None
    if rows is not None and not rows.empty:
        vals = rows[list(RATIO_COLUMNS)].apply(pd.to_numeric, errors="coerce").astype("float64")
        n = int(vals.notna().any(axis=1).sum())
        for col in RATIO_COLUMNS:
            m = vals[col].median()
            median[col] = None if pd.isna(m) else float(m)
        ds = pd.to_datetime(rows["date"])
        # 종목별 '최신 행' 이라 스냅샷이 빠진 종목의 옛 날짜가 섞일 수 있다 → 범위를 드러낸다
        dates = {"min": ds.min().date().isoformat(), "max": ds.max().date().isoformat()}
    else:
        median = dict.fromkeys(RATIO_COLUMNS)
    return {"sector": sector, "n": n, "median": median, "dates": dates}


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


def _ratios_by_year_us(edgar: Any, symbol: str, years: list[int]) -> dict[int, dict]:
    """연도별 비율(US). EDGAR 조회 실패는 빈 dict (가격 지표 행은 그대로 저장한다)."""
    from trader.sources.edgar import ratios_by_year_us

    try:
        return ratios_by_year_us(edgar, symbol, years)
    except Exception as e:  # noqa: BLE001 - EDGAR 한도·네트워크 등
        logger.warning("EDGAR 재무 %s 실패: %s", symbol, type(e).__name__)
        return {}


def _ratios_by_year(
    market: Market, ticker: str, years: list[int], *, dart: Any | None, edgar: Any | None
) -> dict[int, dict] | None:
    if market == "KR":
        return _ratios_by_year_kr(dart, ticker, years) if dart else None
    return _ratios_by_year_us(edgar, ticker, years) if edgar else None


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
    edgar: Any | None = None,
    state: Path | None = None,
    progress: Callable[[str], None] = lambda s: None,
) -> int:
    """유니버스 전 종목의 과거 피처를 재구성해 저장한다. 종목 실패는 로그 후 계속. 반환은 행 수.

    비율은 KR 은 DART(`dart`), US 는 EDGAR(`edgar`) 연도별 값이며 클라이언트가 None 이면 NaN."""
    days = min(years * 252, MAX_DAYS)
    if years * 252 > MAX_DAYS:
        logger.warning("일봉 상한 %d봉으로 제한합니다 (요청 %d년)", MAX_DAYS, years)
    total = 0
    frames: list[pd.DataFrame] = []
    touched: set[Path] = set()

    def flush() -> None:
        # 종목마다 write_rows 를 부르면 날짜 파일을 종목 × 날짜 만큼 읽고 다시 쓴다 → 묶음으로 1회.
        # 쓰기가 실패하면 모은 행을 버리지 않고 다음 flush 에서 다시 시도한다 (조용한 유실 방지).
        nonlocal total
        if not frames:
            return
        batch = pd.concat(frames, ignore_index=True)
        try:
            # 파생 열은 묶음마다 계산하지 않고(날짜 × 묶음 수만큼 느려진다) 끝에 건드린 파일만 1회
            touched.update(write_rows(batch, state, derive=False))
        except Exception as e:  # noqa: BLE001 - 쓰기 실패는 다음 묶음과 함께 재시도
            logger.warning(
                "백필 묶음 쓰기 실패 (%d행 보류, 다음 묶음에서 재시도): %s",
                len(batch),
                e,
            )
            return
        frames.clear()
        total += len(batch)

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
            ratios = _ratios_by_year(market, ticker, fy_years, dart=dart, edgar=edgar)
            df = history_rows(ticker=ticker, market=market, candles=candles, ratios_by_year=ratios)
            frames.append(df)
            progress(f"{market} {ticker}: {len(df)}행")
            if len(frames) >= BACKFILL_CHUNK:
                flush()
        except Exception as e:  # noqa: BLE001 - 종목 단위 격리
            logger.warning("백필 실패 %s %s: %s: %s", market, ticker, type(e).__name__, e)
            progress(f"{market} {ticker}: 실패 ({type(e).__name__})")
    flush()
    if frames:
        logger.error("백필 마지막 묶음 쓰기 실패: %d종목 행이 저장되지 않음", len(frames))
        progress(f"쓰기 실패: {len(frames)}종목 미저장")
    if touched:
        n_files = rederive(state, paths=sorted(touched))
        progress(f"파생 열 계산: {n_files}개 날짜 파일")
    return total


def counts_from_inputs(
    ticker: str, market: Market, asof: datetime, state: Path | None = None
) -> tuple[int | None, int | None]:
    """같은 KST 날짜의 00_inputs 에서 뉴스·커뮤니티 건수를 센다. 없으면 (None, None)."""
    from trader.orchestrator.run_store import read_checkpoint, run_date_for
    from trader.platform import paths

    base = state if state is not None else paths.state_dir()
    # US 분석은 ET 운영일 디렉터리에 있다 (KST 날짜로 찾으면 스냅샷 시각엔 아직 없어 늘 NaN)
    d = base / "runs" / run_date_for(market, asof).isoformat() / market / ticker
    inputs = read_checkpoint(d, "00_inputs") if d.is_dir() else None
    if not inputs:
        return None, None

    def _count(section: Any, *keys: str) -> int | None:
        data = (section or {}).get("data") if isinstance(section, dict) else None
        if not isinstance(data, dict):
            return None
        for k in keys:
            if isinstance(data.get(k), list):
                return len(data[k])
        return None

    news = _count(((inputs.get("news_macro") or {}).get("news")), "items", "articles", "news")
    posts = _count(((inputs.get("sentiment") or {}).get("community")), "posts")
    return news, posts


def snapshot(
    *,
    market_client: Any,
    dart: Any | None,
    universe: Universe,
    markets: list[Market],
    asof: datetime | None = None,
    edgar: Any | None = None,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str], None] = lambda s: None,
) -> int:
    """오늘 행(종목당 1행)을 저장한다. 뉴스·커뮤니티 건수는 같은 날 00_inputs 에서 센다."""
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
            if int(candles[-1].volume) <= 0:
                # 개장 전 스텁 봉: 전날 날짜로 다시 쓰면 어제 행을 오늘 건수로 덮어쓴다 → 건너뜀
                progress(f"{market} {ticker}: 오늘 봉 미완성(거래량 0), 생략")
                continue
            flows = market_client.get_investor_kr(ticker, days=20) if market == "KR" else None
            by_year = _ratios_by_year(
                market, ticker, [_effective_fy(candles[-1].date)], dart=dart, edgar=edgar
            )
            ratios = next(iter(by_year.values()), None) if by_year else None
            news_count, community_count = counts_from_inputs(ticker, market, asof, state)
            rows.append(
                snapshot_row(
                    ticker=ticker,
                    market=market,
                    candles=candles,
                    flows=flows,
                    ratios=ratios,
                    news_count=news_count,
                    community_count=community_count,
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
