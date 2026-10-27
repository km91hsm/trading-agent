"""피처 스토어: state/features/<date>.parquet, 키 (date, ticker), 쓰기는 멱등.

- 지표는 indicators.indicator_frame 그대로(워밍업 NaN 유지).
- 재무비율은 사업연도 Y 값을 Y+1 년 4월 1일부터 적용(룩어헤드 방지). US 비율은 Phase 7 (NaN).
- 투자자동향은 KIS 가 최근 30영업일만 주므로 백필엔 없고(NaN) 스냅샷부터 누적된다.
- 과거 asof 스냅샷은 금지(수집기는 현재 데이터만 준다).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trader.broker.models import Candle, InvestorFlow, Market
from trader.broker.models import drop_incomplete_tail as drop_incomplete_tail  # 정본은 models
from trader.config import ConfigError, SectorMap, Universe, load_sectors
from trader.platform import paths
from trader.platform.filelock import locked
from trader.platform.fsutil import atomic_replace
from trader.sources import indicators
from trader.sources.base import MAX_PAST as _MAX_PAST
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


MAX_PAST = _MAX_PAST  # 정본은 sources/base (inputs.collect_and_save 와 공유)
STORE_LOCK_TIMEOUT_S = 120.0  # 다른 프로세스의 날짜 파일 쓰기(묶음 1회) 대기 상한


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


def store_mtime(state: Path | None) -> float:
    """피처 디렉터리 mtime(날짜 파일 추가·교체 때 바뀐다 — 원자 교체도 이름 바꾸기라 갱신). 캐시의
    무효화 키로 쓴다(성과·위험 카드·최신 행). 디렉터리가 없으면 0. 디렉터리를 만들지 않는다."""
    base = state if state is not None else paths.state_dir()
    try:
        return (base / "features").stat().st_mtime
    except OSError:
        return 0.0


def has_files(state: Path | None) -> bool:
    """날짜 파일이 하나라도 있는가 (데몬 부트스트랩의 백필 필요 판단; 디렉터리는 만들지 않음)."""
    base = state if state is not None else paths.state_dir()
    d = base / "features"
    return d.exists() and any(d.glob("*.parquet"))


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
        [sectors.sector_of(str(t), m) for t, m in zip(out["ticker"], out["market"], strict=True)],
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


_LATEST_MEMO: dict[tuple, pd.DataFrame] = {}
_LATEST_MEMO_MAX = 64
_LATEST_LOCK = threading.Lock()


def latest_rows(
    state: Path | None,
    market: str | None,
    *,
    tickers: list[str] | None = None,
    asof: date | None = None,
    max_files: int = 60,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """종목별 최신 행. 파일(`<date>.parquet`)을 최신부터 읽되 asof 이후 파일은 건너뛰고,
    tickers 가 전부 모이면(또는 max_files 에 닿으면) 멈춘다 — parquet 전부를 읽지 않는다.

    columns 를 주면 그 열 + 키 열(`KEY_COLUMNS`)만 읽고 돌려준다(전체 검토 3차 — 스토어에 없는
    종목이 섞이면 max_files 개를 다 읽기 때문에 열을 줄이는 효과가 크다). 결과는 (디렉터리 경로·
    mtime·파일 수·시장·asof·tickers·columns) 로 기억해 같은 입력의 반복 호출은 파일을 다시 읽지
    않는다 — 날짜 파일을 쓰면 디렉터리 mtime 이 바뀌어 무효가 된다. 돌려주는 DataFrame 은 복사본."""
    fdir = _features_dir(state)
    files = sorted(fdir.glob("*.parquet"), reverse=True) if fdir.exists() else []
    if asof is not None:
        files = [f for f in files if f.stem <= asof.isoformat()]
    files = files[:max_files]
    keep = None if columns is None else list(dict.fromkeys([*KEY_COLUMNS, *columns]))
    memo_key = (
        str(fdir),
        store_mtime(state),
        len(files),
        files[0].stem if files else "",
        market,
        asof,
        None if tickers is None else tuple(sorted(set(tickers))),
        None if keep is None else tuple(keep),
    )
    with _LATEST_LOCK:
        hit = _LATEST_MEMO.get(memo_key)
    if hit is not None:
        return hit.copy()
    out = _latest_rows_read(files, market, tickers, keep)
    with _LATEST_LOCK:
        if len(_LATEST_MEMO) >= _LATEST_MEMO_MAX:
            _LATEST_MEMO.pop(next(iter(_LATEST_MEMO)))  # 가장 먼저 넣은 것부터
        _LATEST_MEMO[memo_key] = out
    return out.copy()


def _latest_rows_read(
    files: list[Path], market: str | None, tickers: list[str] | None, keep: list[str] | None
) -> pd.DataFrame:
    want = set(tickers) if tickers else None
    seen: set[str] = set()
    parts: list[pd.DataFrame] = []
    for f in files:
        # 이전 Phase 에 쓰인 파일은 뒤에 추가된 열이 없을 수 있다 → NaN 으로 맞춘다
        raw = read_part(f, columns=keep)
        if raw is None:
            continue
        part = _coerce(raw.reindex(columns=list(FEATURE_COLUMNS)))
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
        empty = pd.DataFrame(columns=FEATURE_COLUMNS)
        return empty if keep is None else empty[keep]
    out = _coerce(pd.concat(parts, ignore_index=True).sort_values("ticker").reset_index(drop=True))
    return out if keep is None else out[keep]


def known_tickers(state: Path | None, market: str, *, asof: date | None = None) -> set[str]:
    """asof 이하 가장 최근 날짜 파일에 있는 그 시장 종목(ticker·market 열만 읽는다). 스토어에 없는
    종목을 `latest_rows` 에 넘기면 max_files 개를 다 읽으므로 호출 전에 거를 때 쓴다."""
    fdir = _features_dir(state)
    files = sorted(fdir.glob("*.parquet"), reverse=True) if fdir.exists() else []
    if asof is not None:
        files = [f for f in files if f.stem <= asof.isoformat()]
    for f in files[:5]:  # 깨진 파일이면 그 앞 파일
        raw = read_part(f, columns=["ticker", "market"])
        if raw is not None:
            return {str(t) for t in raw.loc[raw["market"] == market, "ticker"]}
    return set()


def _write_day_file(
    path: Path, part: pd.DataFrame, sectors: SectorMap, *, derive: bool = True
) -> None:
    part = _coerce(add_sector_relative(part, sectors) if derive else part)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # 프로세스마다 다른 임시 이름
    part.reset_index(drop=True).to_parquet(tmp, index=False)
    atomic_replace(tmp, path)


def read_part(
    path: Path, *, strict: bool = False, columns: Sequence[str] | None = None, **kw: Any
) -> pd.DataFrame | None:
    """날짜 파일 하나를 읽는다. 깨진 파일(쓰기 중 종료 등)은 경고 후 None — 한 파일 때문에 scan 이
    죽지 않는다. 피처 스토어 읽기는 모두 이 함수를 지난다.
    strict=True(쓰기 직전 합치기용)면 **읽기 실패(OSError: 다른 프로세스가 연 파일·권한)는 올린다**
    — None 으로 돌려 '깨진 파일' 로 보고 새 행만으로 덮으면 그날 다른 종목 행이 사라진다(검토 P11).
    파일 내용이 깨진 경우(파싱 오류)만 None.
    columns 를 주면 파일에 있는 그 열만 읽는다(없는 열은 빠진다 — 호출부가 reindex 로 NaN). 이
    경로는 pyarrow `ParquetFile` 을 직접 써서 pandas dataset 경로보다 파일당 비용이 작다."""
    try:
        if columns is not None and not kw:
            with pq.ParquetFile(path) as pf:
                names = set(pf.schema_arrow.names)
                return pf.read(columns=[c for c in columns if c in names]).to_pandas()
        return pd.read_parquet(path, columns=columns, **kw)
    except OSError as e:
        if strict:
            raise
        logger.warning("피처 파일을 읽지 못해 건너뜀 %s: %s: %s", path.name, type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001 - pyarrow 오류 종류가 많다
        logger.warning("피처 파일을 읽지 못해 건너뜀 %s: %s: %s", path.name, type(e).__name__, e)
        return None


def _store_lock(state: Path | None):
    """스토어 쓰기 락(`state/features.lock`): 데몬 백필과 CLI·세션 도구 스냅샷이 같은 날짜 파일을
    동시에 읽고-합치고-쓰면 한쪽 행이 사라진다."""
    return locked(_features_dir(state).parent / "features.lock", timeout=STORE_LOCK_TIMEOUT_S)


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
        raw = read_part(f, filters=[("ticker", "==", ticker)])  # 행 필터 뒤에 변환
        if raw is None:
            continue
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
    with _store_lock(state):
        for day, part in df.groupby("date", sort=True):
            path = d / f"{pd.Timestamp(day).date().isoformat()}.parquet"
            if path.exists():
                # 이전 Phase 파일은 열이 적을 수 있다 (latest_rows 와 같은 reindex)
                raw = read_part(path, strict=True)  # 읽기 실패는 올린다(덮어쓰기 금지)
                if raw is not None:  # 깨진 파일은 새 행으로 덮는다
                    old = _coerce(raw.reindex(columns=FEATURE_COLUMNS))
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
    n = 0
    for path in files:
        # 파일마다 락을 잡고 놓는다: 진행 콜백(데몬은 여기서 heavy 락을 양보)이 파일 락을 쥔 채
        # 불리면 그사이 도는 reflect 스냅샷이 LockTimeout 으로 실패한다(전체 검토 P9)
        with _store_lock(state):
            raw = read_part(path)
            if raw is None:
                continue
            part = _coerce(raw.reindex(columns=FEATURE_COLUMNS))
            _write_day_file(path, part, sectors)
        progress(f"{path.stem}: {len(part)}행")
        n += 1
    return n


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
    rows = (
        latest_rows(state, market, tickers=tickers, asof=asof, columns=list(RATIO_COLUMNS))
        if tickers
        else None
    )
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
    only: list[tuple[str, str | None]] | None = None,
) -> int:
    """유니버스 전 종목의 과거 피처를 재구성해 저장한다. 종목 실패는 로그 후 계속. 반환은 행 수.
    `only=[(코드, 거래소)]` 면 그 종목만(수정주가 재백필, 시장은 `markets[0]`).

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

    targets = (
        [(t, markets[0], ex) for t, ex in only] if only is not None else _targets(universe, markets)
    )
    for ticker, market, exchange in targets:
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
            if only is not None:
                df = _keep_observed(df, state, market, ticker)
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
        # 실패한 쓰기가 일부 날짜 파일을 derive=False 로 남겼을 수 있다(touched 에 없음)
        # → 전체 재계산
        n_files = rederive(state)
        progress(f"파생 열 계산(전체): {n_files}개 날짜 파일")
    elif touched:
        n_files = rederive(state, paths=sorted(touched))
        progress(f"파생 열 계산: {n_files}개 날짜 파일")
    return total


def _keep_observed(df: pd.DataFrame, state: Path | None, market: str, ticker: str) -> pd.DataFrame:
    """재백필은 가격·지표만 새로 쓴다: `history_rows` 가 채우지 못하는 관측 열(수급·뉴스·커뮤니티
    건수 등 — 새 표에서 전부 NaN)은 저장된 값을 같은 날짜에 되살린다(행 교체로 지워지지 않게)."""
    from trader.ml import dataset

    since = pd.Timestamp(df["date"].min()).date() if not df.empty else None
    try:
        old = dataset.load_all(state, market=market, since=since)
    except Exception:  # noqa: BLE001 - 읽지 못하면 그대로(재백필이 우선)
        return df
    if old.empty:
        return df
    old = old[old["ticker"] == ticker]
    if old.empty:
        return df
    old = old.assign(date=pd.to_datetime(old["date"]).dt.date).set_index("date")
    out = df.copy()
    keys = pd.to_datetime(out["date"]).dt.date
    for col in out.columns:
        if col in ("date", "ticker", "market") or col not in old.columns:
            continue
        if out[col].isna().all():
            out[col] = keys.map(old[col]).to_numpy()
    return out


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
    tickers: list[tuple[str, str | None]] | None = None,
    on_adjusted: Callable[[str, list[str]], None] | None = None,
) -> int:
    """오늘 행(종목당 1행)을 저장한다. 뉴스·커뮤니티 건수는 같은 날 00_inputs 에서 센다.
    `tickers=[(코드, 거래소)]` 를 주면 유니버스 대신 그 종목만(유니버스 밖 발굴 종목의 당일 1회
    스냅샷, Phase 21) — 시장은 `markets[0]`.

    수정주가 감지(Stock D-034·D-064·D-070): 분할·병합이 나면 KIS 는 과거 일봉을 새 기준으로 다시
    준다. 받은 일봉의 최근 `ADJUST_WINDOW` 봉(마지막 봉 제외)이 저장 종가와 `ADJUST_TOL` 넘게
    다르면 그 종목만 `backfill` 로 다시 쓰고(가격 절벽·라벨 오염 방지) `on_adjusted(시장, 종목)`."""
    asof = asof or now()
    if now() - asof > MAX_PAST:
        raise ConfigError(
            f"과거 asof 스냅샷은 Phase 7 백테스트에서 지원합니다 (asof={asof.isoformat()})"
        )
    rows: list[dict] = []
    if tickers is not None:
        targets = [(t, markets[0], ex) for t, ex in tickers]
    else:
        targets = _targets(universe, markets)
    stored = _stored_closes(state, {m for _, m, _ in targets}, since=asof.date() - timedelta(400))
    adjusted: dict[str, list[tuple[str, str | None]]] = {}
    for ticker, market, exchange in targets:
        try:
            candles = (
                market_client.get_daily_kr(ticker, days=260)
                if market == "KR"
                else market_client.get_daily_us(ticker, exchange, days=260)
            )
            if not candles:
                raise ValueError("일봉 없음")
            if _history_changed(candles, stored.get((market, ticker), {})):
                adjusted.setdefault(market, []).append((ticker, exchange))
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
    written = 0
    if rows:
        df = pd.DataFrame(rows)[FEATURE_COLUMNS]
        write_rows(df, state)
        written = len(df)
    for market, items in adjusted.items():
        names = [t for t, _ in items]
        logger.warning("수정주가 변경 감지 %s %s — 재백필", market, ", ".join(names))
        progress(f"{market} 수정주가 변경: {', '.join(names)} 재백필")
        backfill(
            market_client=market_client,
            dart=dart,
            universe=universe,
            markets=[market],  # type: ignore[list-item]
            edgar=edgar,
            state=state,
            progress=progress,
            only=items,
        )
        if on_adjusted is not None:
            on_adjusted(market, names)
    return written


ADJUST_TOL = 0.005
ADJUST_WINDOW = 30
# 분할·병합은 이벤트 전 모든 봉을 바꾼다 — 장중에 찍힌 스냅샷 종가와 확정 종가의 차이(한두 봉)를
# 수정주가로 오인하지 않게 여러 봉이 함께 달라야 한다(Codex 리뷰)
ADJUST_MIN_BARS = 3


def _history_changed(candles: list[Candle], stored: dict[date, float]) -> bool:
    """최근 ADJUST_WINDOW 봉(마지막 봉 제외 — 장중이면 아직 변한다) 중 ADJUST_MIN_BARS 봉 이상이
    저장 종가와 ADJUST_TOL 넘게 다른가."""
    n = 0
    for c in candles[-ADJUST_WINDOW - 1 : -1]:
        s = stored.get(c.date)
        if s and s > 0 and abs(float(c.close) / s - 1) > ADJUST_TOL:
            n += 1
    return n >= ADJUST_MIN_BARS


def _stored_closes(
    state: Path | None, markets: set[str], *, since: date
) -> dict[tuple[str, str], dict[date, float]]:
    """(시장, 종목) → {날짜: 저장 종가}. 스토어가 비었거나 읽기 실패면 빈 맵(감지 생략)."""
    from trader.ml import dataset  # dataset 이 features 를 가져다 쓴다(순환 방지)

    out: dict[tuple[str, str], dict[date, float]] = {}
    for m in markets:
        try:
            df = dataset.load_all(state, market=m, since=since)
        except Exception as e:  # noqa: BLE001 - 감지는 보조: 실패해도 스냅샷은 간다
            logger.warning("저장 종가 읽기 실패 %s: %s", m, e)
            continue
        if df.empty:
            continue
        for t, part in df[["ticker", "date", "close"]].groupby("ticker"):
            out[(m, str(t))] = {
                pd.Timestamp(d).date(): float(c)
                for d, c in zip(part["date"], part["close"], strict=True)
                if pd.notna(c)
            }
    return out
