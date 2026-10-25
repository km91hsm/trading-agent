"""애널리스트 입력 00_inputs.json 조립: 모든 수집기를 Section 단위로 묶는다.

키 배치(Phase 3 애널리스트가 읽는 구조):
  technical{quote, daily, minute, orderbook, indicators}
  fundamental{ratios, disclosures}
  news_macro{news, macro(항목별 Section dict)}
  sentiment{investor_flow, short_interest, community, trends, manual_inputs}
  + meta + errors
저장 전에 Decimal → 문자열, 날짜 → ISO 로 바꾼다. 구조를 바꾸면 SCHEMA_VERSION 을 올린다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from pydantic import BaseModel

from trader.broker.models import drop_incomplete_tail
from trader.config import (
    KST,
    MARKET_TZ,
    ConfigError,
    Market,
    Secrets,
    SourcesConfig,
    load_sectors,
    load_universe,
)
from trader.orchestrator.run_store import read_checkpoint, run_date_for, run_dir, write_checkpoint
from trader.sources import indicators, manual_inputs
from trader.sources.base import SKIP_REASONS, Section, run_section, skipped_section
from trader.sources.community import collect_channels, collect_community  # noqa: F401
from trader.sources.dart import ratios_kr
from trader.sources.edgar import financial_history_us, ratios_from_facts
from trader.sources.macro import collect_macro
from trader.sources.news import collect_news

SECTION_TIMEOUT_S = 180.0  # 절(fundamental·news_macro·sentiment) 하나의 최대 대기

logger = logging.getLogger("trader.sources")

# v2: fundamental.history (3개년 + 최근 분기), v3: fundamental.peer (동종업 중앙값)
SCHEMA_VERSION = 3
MAX_PAST = timedelta(hours=24)
MAX_FUTURE = timedelta(hours=24)  # 과거와 대칭. 연도 오타 같은 먼 미래만 거부


@dataclass
class InputDeps:
    """수집에 쓰는 외부 클라이언트 묶음. None 이면 해당 절은 건너뛴다."""

    market: Any | None
    dart: Any | None
    edgar: Any | None
    http: httpx.Client | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _jsonable(obj.model_dump(mode="json"))
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def _is_section(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"data", "received_at", "error"}


_SUB_SOURCE_OK = frozenset({"ok", "not available for KR", "not available for US"})


def _collect_errors(inputs: dict) -> list[str]:
    """error 가 있는 절을 '<역할>.<키>: <오류>' 목록으로 모은다 (SKIP_REASONS 제외).

    뉴스·커뮤니티처럼 절 안에 소스별 결과(`data.sources`)가 있으면 그 하위 실패도
    '<역할>.<키>.<소스>: <오류>' 로 덧붙인다 (네이버 401, Reddit 429 등).
    """
    out: list[str] = []

    def visit(path: str, v: Any) -> None:
        if _is_section(v):
            if v["error"] is not None and v["error"] not in SKIP_REASONS:
                out.append(f"{path}: {v['error']}")
            elif isinstance(v["data"], dict) and isinstance(v["data"].get("sources"), dict):
                for src, status in v["data"]["sources"].items():
                    if status not in _SUB_SOURCE_OK:
                        out.append(f"{path}.{src}: {status}")
        elif isinstance(v, dict):
            for k, sub in v.items():
                visit(f"{path}.{k}", sub)

    for role in ("technical", "fundamental", "news_macro", "sentiment"):
        for key, val in inputs[role].items():
            visit(f"{role}.{key}", val)
    return out


def _dump(items: list) -> list[dict]:
    return [i.model_dump(mode="json") for i in items]


def _quote_or_raise(q: Any) -> dict:
    if q.price is None or Decimal(str(q.price)) <= 0:
        raise ValueError(f"가격이 0 이거나 없습니다: {q.ticker}")
    return q.model_dump(mode="json")


def _asof_date(market: str, asof: datetime) -> date:
    """룩어헤드 컷오프: asof 의 시장 현지 날짜. 이 날짜 뒤의 봉·수급은 수집에서 뺀다."""
    return asof.astimezone(MARKET_TZ.get(market, KST)).date()


def _technical(ticker, market, exchange, cfg, deps, asof: datetime) -> dict[str, Section]:
    now = deps.now
    m = deps.market
    keys = ("quote", "daily", "minute", "orderbook", "indicators")
    if m is None:
        return {k: skipped_section("no market client", now=now) for k in keys}
    kr = market == "KR"
    cutoff = _asof_date(market, asof)
    candles: list = []

    def daily() -> list[dict]:
        nonlocal candles
        got = (
            m.get_daily_kr(ticker, days=cfg.kis.daily_days)
            if kr
            else m.get_daily_us(ticker, exchange, days=cfg.kis.daily_days)
        )
        # 과거 asof 재수집(24시간 안)이면 KIS 는 현재 기준 봉을 주므로 asof 뒤 봉을 잘라 낸다.
        # 개장 전 거래량 0 스텁 봉은 지표(이동평균·RSI)를 가짜 종가로 틀어 놓으므로 버린다
        candles = drop_incomplete_tail([c for c in got if c.date <= cutoff])
        if not candles:
            raise ValueError("일봉이 비어 있습니다")
        return _dump(candles)

    def minute() -> list[dict]:
        got = m.get_minute_kr(ticker) if kr else m.get_minute_us(ticker, exchange)
        return _dump([c for c in got if c.date <= cutoff])

    out: dict[str, Section] = {
        "quote": run_section(
            lambda: _quote_or_raise(
                m.get_price_kr(ticker) if kr else m.get_price_us(ticker, exchange)
            ),
            now=now,
        ),
        "daily": run_section(daily, now=now),
        "minute": run_section(minute, now=now),
        "orderbook": run_section(
            lambda: (
                m.get_orderbook_kr(ticker) if kr else m.get_orderbook_us(ticker, exchange)
            ).model_dump(mode="json"),
            now=now,
        ),
    }
    out["indicators"] = (
        run_section(lambda: indicators.latest(candles), now=now)
        if out["daily"]["error"] is None
        else skipped_section("no daily candles", now=now)
    )
    return out


def _peer_section(ticker: str, market: Market, asof: datetime, now) -> Section:
    """스펙 §3 '동종업 중앙값 대비': 피처 스토어 최신 행(그 날 이하)으로 섹터 중앙값·내 값·차이.

    섹터를 모르거나 구성원이 3개 미만이면 `no sector peers` 로 건너뛴다 (errors 집계 제외)."""
    from trader.ml import features

    day = run_date_for(market, asof)  # 룩어헤드 규칙은 시장 현지 날짜 (US 는 뉴욕)

    def peer() -> dict | None:
        sectors = load_sectors()
        sector = sectors.sector_of(ticker, market)
        if sector is None:
            return None
        universe = load_universe()
        members = universe.kr if market == "KR" else [s.symbol for s in universe.us]
        members = sorted({t for t in members if sectors.sector_of(t, market) == sector} | {ticker})
        stats = features.sector_medians(None, market, sector, tickers=members, asof=day)
        if stats["n"] < features.MIN_SECTOR_PEERS:
            return None
        mine_rows = features.latest_rows(None, market, tickers=[ticker], asof=day)
        mine: dict[str, float | None] = dict.fromkeys(features.RATIO_COLUMNS)
        if not mine_rows.empty:
            for col in features.RATIO_COLUMNS:
                v = mine_rows.iloc[0][col]
                mine[col] = None if v is None or v is pd.NA or pd.isna(v) else float(v)
        names = features.RATIO_COLUMNS  # 열 → 한글 비율 키 (basic_ratios 와 같은 키)
        median = {names[c]: stats["median"][c] for c in names}
        mine_k = {names[c]: mine[c] for c in names}
        diff = {
            k: (None if mine_k[k] is None or median[k] is None else mine_k[k] - median[k])
            for k in median
        }
        return {
            "sector": sector,
            "n": stats["n"],
            "dates": stats["dates"],
            "median": median,
            "mine": mine_k,
            "diff": diff,
        }

    sec = run_section(peer, now=now)
    if sec["error"] is None and sec["data"] is None:
        return skipped_section("no sector peers", now=now)
    return sec


def _fundamental(ticker, market, asof, cfg, deps, technical) -> dict[str, Section]:
    now = deps.now
    quote = technical["quote"]["data"]
    price = Decimal(str(quote["price"])) if quote else None
    day = asof.astimezone(KST).date()
    keys = ("ratios", "history", "disclosures")
    peer = _peer_section(ticker, market, asof, now)
    if market == "KR":
        if not cfg.dart.enabled:
            return {k: skipped_section("disabled", now=now) for k in keys} | {"peer": peer}
        if deps.dart is None:
            return {k: skipped_section("no dart client", now=now) for k in keys} | {"peer": peer}

        def history() -> dict:
            code = deps.dart.corp_code(ticker)
            if not code:
                raise ValueError(f"DART 고유번호 없음: {ticker}")
            return deps.dart.financial_history(code, day)

        def disclosures() -> list[dict]:
            code = deps.dart.corp_code(ticker)
            return deps.dart.disclosures(code, asof, days=30) if code else []

        return {
            "ratios": run_section(lambda: ratios_kr(deps.dart, ticker, day, price), now=now),
            "history": run_section(history, now=now),
            "disclosures": run_section(disclosures, now=now),
            "peer": peer,
        }
    if not cfg.edgar.enabled:
        ratios = history = skipped_section("disabled", now=now)
    elif deps.edgar is None:
        ratios = history = skipped_section("no edgar client", now=now)
    else:
        cache: dict[str, dict] = {}

        def facts() -> dict:
            # companyfacts 는 이 수집 안에서 1회 (클라이언트에 프로세스 수명 메모를 두지 않는다)
            if "f" not in cache:
                cik = deps.edgar.cik(ticker)
                if not cik:
                    raise ValueError(f"EDGAR CIK 없음: {ticker}")
                cache["f"] = deps.edgar.companyfacts(cik)
            return cache["f"]

        ratios = run_section(lambda: ratios_from_facts(facts(), day, price), now=now)
        history = run_section(lambda: financial_history_us(facts(), day), now=now)
    return {
        "ratios": ratios,
        "history": history,
        "disclosures": skipped_section("phase6", now=now),
        "peer": peer,
    }


def _news_macro(ticker, market, name, asof, cfg, secrets, deps) -> dict[str, Any]:
    now = deps.now
    news = run_section(
        lambda: collect_news(
            cfg.news,
            secrets,
            ticker=ticker,
            name=name,
            market=market,
            asof=asof,
            http=deps.http,
            now=now,
        ),
        enabled=cfg.news.enabled,
        now=now,
    )
    macro: Any
    if cfg.macro.enabled:
        macro = _macro_memoized(cfg, secrets, market, asof, deps, now)
    else:
        macro = skipped_section("disabled", now=now)
    return {"news": news, "macro": macro}


MACRO_MEMO_TTL = timedelta(minutes=15)
_MACRO_MEMO: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
_MACRO_MEMO_LOCK = threading.Lock()


def _macro_memoized(cfg, secrets, market: str, asof: datetime, deps, now) -> dict[str, Any]:
    """거시 지표(FRED·ECOS·지수)는 종목과 무관하다 — 같은 시장·운영일의 수집은 15분 동안 재사용해
    종목마다 8회씩 외부 API 를 다시 부르지 않는다. 전부 실패한 결과는 메모하지 않는다."""
    key = (market, asof.astimezone(UTC).date().isoformat())
    t = now()
    with _MACRO_MEMO_LOCK:
        hit = _MACRO_MEMO.get(key)
        if hit is not None and t - hit[0] < MACRO_MEMO_TTL:
            return hit[1]
    macro = collect_macro(
        cfg.macro, secrets, market_client=deps.market, asof=asof, http=deps.http, now=now
    )
    if isinstance(macro, dict) and any(
        (s.get("error") if isinstance(s, dict) else getattr(s, "error", None)) is None
        for s in macro.values()
    ):
        with _MACRO_MEMO_LOCK:
            _MACRO_MEMO[key] = (t, macro)
    return macro


_EVENT_KINDS = (
    ("실적", "earnings"),
    ("배당", "dividend"),
    ("유상증자", "offering"),
    ("무상증자", "bonus"),
    ("자기주식", "buyback"),
    ("합병", "merger"),
    ("분할", "split"),
    ("소송", "lawsuit"),
)


def _events(fundamental: dict, asof: datetime, now) -> Section:
    """최근 7일 공시를 이벤트 종류로 분류한다. 예정 이벤트 소스는 아직 없어 upcoming 은 빈 목록."""

    def build() -> dict:
        disc = fundamental.get("disclosures") or {}
        items = disc.get("data") or []  # Section 은 TypedDict(dict)
        since = (asof - timedelta(days=7)).date().isoformat()
        recent = []
        for it in items:
            # DART disclosures() 는 rcept_dt, EDGAR 는 filed 를 준다
            d = str(it.get("date") or it.get("rcept_dt") or it.get("filed") or "")[:10]
            if d and d < since:
                continue
            title = str(it.get("title") or "")
            kind = next((k for word, k in _EVENT_KINDS if word in title), "other")
            recent.append({"date": d, "kind": kind, "title": title, "source": it.get("source")})
        return {"recent": recent, "upcoming": [], "note": "예정 이벤트(실적일·배당락) 소스 미확정"}

    return run_section(build, now=now)


def _sentiment(ticker, market, name, asof, cfg, deps, secrets=None) -> dict[str, Section]:
    now = deps.now
    if market != "KR":
        flow = skipped_section("not available for US", now=now)
    elif deps.market is None:
        flow = skipped_section("no market client", now=now)
    else:
        cutoff = _asof_date(market, asof)
        flow = run_section(
            lambda: _dump(
                [
                    f
                    for f in deps.market.get_investor_kr(ticker, days=cfg.kis.investor_days)
                    if f.date <= cutoff
                ]
            ),
            now=now,
        )
    return {
        "investor_flow": flow,
        "short_interest": skipped_section("phase6", now=now),
        "community": run_section(
            lambda: collect_community(
                cfg.community,
                ticker=ticker,
                name=name,
                market=market,
                asof=asof,
                http=deps.http,
                now=now,
                reddit_auth=_reddit_auth(secrets),
                channels=cfg.channels,
            ),
            enabled=cfg.community.enabled,
            now=now,
        ),
        "trends": skipped_section("phase8", now=now),
        "manual_inputs": _manual(ticker, market, asof, now),
    }


def _reddit_auth(secrets: Secrets | None) -> tuple[str, str, str] | None:
    if secrets and secrets.REDDIT_CLIENT_ID and secrets.REDDIT_CLIENT_SECRET:
        return (secrets.REDDIT_CLIENT_ID, secrets.REDDIT_CLIENT_SECRET, secrets.REDDIT_USER or "")
    return None


def _manual(ticker: str, market: str, asof: datetime, now) -> Section:
    """종목 파일 + 시장 공통 파일(`_market_<시장>.md`, asof 이전 항목만). 둘 다 없으면 건너뜀."""
    data = manual_inputs.read(ticker)
    notes = manual_inputs.read_market(market, asof=asof)
    if data is None and not notes:
        return skipped_section("no manual input", now=now)
    if data is not None and data.get("error"):
        return Section(data=None, received_at=now().isoformat(), error=data["error"])
    merged = dict(data or {"format": "market"})
    merged["market_notes"] = notes
    return Section(data=merged, received_at=now().isoformat(), error=None)


def _name(ticker: str, market: Market, deps: InputDeps) -> str:
    """종목명. KR 은 DART → KIS 순, US 는 EDGAR 티커 맵. 전부 실패하면 ticker.

    DART 는 점검(800)이 잦아 KIS 폴백이 상시 경로다 (실기 확인).
    """
    lookups: list[Callable[[], str | None]] = []
    if market == "KR":
        if deps.dart is not None:
            lookups.append(lambda: deps.dart.corp_name(ticker))
        if deps.market is not None and hasattr(deps.market, "get_name_kr"):
            lookups.append(lambda: deps.market.get_name_kr(ticker))
    elif deps.edgar is not None and hasattr(deps.edgar, "company_name"):
        lookups.append(lambda: deps.edgar.company_name(ticker))
    for fn in lookups:
        try:
            name = fn()
        except Exception as e:  # noqa: BLE001 - 이름 조회 실패는 다음 소스로
            logger.warning("종목명 조회 실패 %s: %s", ticker, type(e).__name__)
            continue
        if name:
            return str(name).strip()
    return ticker


def build_inputs(
    *,
    ticker: str,
    market: Market,
    exchange: str | None,
    asof: datetime,
    cfg: SourcesConfig,
    secrets: Secrets,
    deps: InputDeps,
) -> dict:
    """종목 하나의 00_inputs 구조를 조립한다. 절 실패는 error 로 기록하고 계속한다."""
    if market == "US" and not exchange:
        raise ConfigError("미국 종목은 exchange(거래소 코드, NAS/NYS/AMS)가 필요합니다")
    name = _name(ticker, market, deps)
    technical = _technical(ticker, market, exchange, cfg, deps, asof)
    daily_rows = technical["daily"]["data"] or []
    result: dict[str, Any] = {
        "meta": {
            "ticker": ticker,
            "market": market,
            "exchange": exchange,
            "name": name,
            "asof": asof.astimezone(UTC).isoformat(),
            # 마지막 일봉 날짜 (장중이면 당일 미완성 봉). 읽는 쪽이 부분 봉 여부를 판단한다
            "last_candle_date": daily_rows[-1]["date"] if daily_rows else None,
            "generated_at": deps.now().isoformat(),
            "schema_version": SCHEMA_VERSION,
        },
        "technical": technical,
    }
    # 세 절은 서로 독립(fundamental 만 technical 의 현재가를 쓴다)이고 네트워크 대기가 대부분이라
    # 동시에 돈다. KIS 클라이언트는 RateLimiter 락, httpx.Client 는 스레드 안전, DART 지연 초기화는
    # 자체 락. 각 절은 run_section 이 예외를 절 안에 가두므로 결과 조립은 그대로다.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="collect") as pool:
        futures = {
            "fundamental": pool.submit(_fundamental, ticker, market, asof, cfg, deps, technical),
            "news_macro": pool.submit(_news_macro, ticker, market, name, asof, cfg, secrets, deps),
            "sentiment": pool.submit(_sentiment, ticker, market, name, asof, cfg, deps, secrets),
        }
        for key, fut in futures.items():
            try:
                result[key] = fut.result(timeout=SECTION_TIMEOUT_S)
            except FuturesTimeout:
                # 한 절이 멈춰도(네트워크 TLS 교착 등) 수집 전체·세션을 막지 않는다. 스레드는 풀이
                # 닫힐 때까지 남지만 결과는 오류 절로 기록한다
                logger.warning("수집 절 %s 시간 초과(%ss) %s", key, SECTION_TIMEOUT_S, ticker)
                result[key] = Section(
                    data=None,
                    received_at=deps.now().isoformat(),
                    error=f"timeout after {SECTION_TIMEOUT_S}s",
                )
    result["news_macro"]["events"] = _events(result["fundamental"], asof, deps.now)
    result = _jsonable(result)
    result["errors"] = _collect_errors(result)
    return result


def collect_and_save(
    *,
    ticker: str,
    market: Market,
    exchange: str | None,
    asof: datetime,
    cfg: SourcesConfig,
    secrets: Secrets,
    deps: InputDeps,
    state: Path | None = None,
    force: bool = False,
) -> Path:
    """00_inputs 체크포인트를 만든다. 있으면 force 가 아닌 한 건너뛴다.

    수집기는 현재 시점 데이터만 주므로 now 보다 24시간 넘게 과거인 asof 는 거부한다(룩어헤드).
    """
    if market == "US" and not exchange:
        raise ConfigError("미국 종목은 exchange(거래소 코드, NAS/NYS/AMS)가 필요합니다")
    if deps.now() - asof > MAX_PAST:
        raise ConfigError(
            f"과거 asof 재수집은 Phase 7 백테스트에서 지원합니다 (asof={asof.isoformat()})"
        )
    if asof - deps.now() > MAX_FUTURE:
        raise ConfigError(f"미래 asof 는 허용하지 않습니다 (asof={asof.isoformat()})")
    # 디렉터리 날짜는 시장 운영일(US 는 뉴욕 날짜): 데몬 monitor·재평가와 같은 runs/<date> 를 본다
    d = run_dir(run_date_for(market, asof), market, ticker, state)
    if not force and read_checkpoint(d, "00_inputs") is not None:
        logger.info("00_inputs 이미 존재, 건너뜀: %s", d)
        return d / "00_inputs.json"
    data = build_inputs(
        ticker=ticker,
        market=market,
        exchange=exchange,
        asof=asof,
        cfg=cfg,
        secrets=secrets,
        deps=deps,
    )
    return write_checkpoint(d, "00_inputs", data)
