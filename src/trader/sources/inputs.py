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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from trader.config import ConfigError, Market, Secrets, SourcesConfig
from trader.orchestrator.run_store import read_checkpoint, run_dir, write_checkpoint
from trader.sources import indicators
from trader.sources.base import SKIP_REASONS, Section, run_section, skipped_section
from trader.sources.community import collect_community
from trader.sources.dart import ratios_kr
from trader.sources.edgar import ratios_us
from trader.sources.macro import collect_macro
from trader.sources.news import collect_news

logger = logging.getLogger("trader.sources")

_KST = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION = 1
MAX_PAST = timedelta(hours=24)


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


def _collect_errors(inputs: dict) -> list[str]:
    """error 가 있는 절을 '<역할>.<키>: <오류>' 목록으로 모은다 (SKIP_REASONS 제외)."""
    out: list[str] = []

    def visit(path: str, v: Any) -> None:
        if _is_section(v):
            if v["error"] is not None and v["error"] not in SKIP_REASONS:
                out.append(f"{path}: {v['error']}")
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


def _technical(ticker, market, exchange, cfg, deps) -> dict[str, Section]:
    now = deps.now
    m = deps.market
    keys = ("quote", "daily", "minute", "orderbook", "indicators")
    if m is None:
        return {k: skipped_section("no market client", now=now) for k in keys}
    kr = market == "KR"
    candles: list = []

    def daily() -> list[dict]:
        nonlocal candles
        candles = (
            m.get_daily_kr(ticker, days=cfg.kis.daily_days)
            if kr
            else m.get_daily_us(ticker, exchange, days=cfg.kis.daily_days)
        )
        if not candles:
            raise ValueError("일봉이 비어 있습니다")
        return _dump(candles)

    out: dict[str, Section] = {
        "quote": run_section(
            lambda: _quote_or_raise(
                m.get_price_kr(ticker) if kr else m.get_price_us(ticker, exchange)
            ),
            now=now,
        ),
        "daily": run_section(daily, now=now),
        "minute": run_section(
            lambda: _dump(m.get_minute_kr(ticker) if kr else m.get_minute_us(ticker, exchange)),
            now=now,
        ),
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


def _fundamental(ticker, market, asof, cfg, deps, technical) -> dict[str, Section]:
    now = deps.now
    quote = technical["quote"]["data"]
    price = Decimal(str(quote["price"])) if quote else None
    day = asof.astimezone(_KST).date()
    if market == "KR":
        if not cfg.dart.enabled:
            return {
                "ratios": skipped_section("disabled", now=now),
                "disclosures": skipped_section("disabled", now=now),
            }
        if deps.dart is None:
            return {
                "ratios": skipped_section("no dart client", now=now),
                "disclosures": skipped_section("no dart client", now=now),
            }
        code = deps.dart.corp_code(ticker)
        return {
            "ratios": run_section(lambda: ratios_kr(deps.dart, ticker, day, price), now=now),
            "disclosures": run_section(
                lambda: deps.dart.disclosures(code, asof, days=30) if code else [], now=now
            ),
        }
    if not cfg.edgar.enabled:
        return {
            "ratios": skipped_section("disabled", now=now),
            "disclosures": skipped_section("phase6", now=now),
        }
    if deps.edgar is None:
        return {
            "ratios": skipped_section("no edgar client", now=now),
            "disclosures": skipped_section("phase6", now=now),
        }
    return {
        "ratios": run_section(lambda: ratios_us(deps.edgar, ticker, day, price), now=now),
        "disclosures": skipped_section("phase6", now=now),
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
        macro = collect_macro(
            cfg.macro, secrets, market_client=deps.market, asof=asof, http=deps.http, now=now
        )
    else:
        macro = skipped_section("disabled", now=now)
    return {"news": news, "macro": macro}


def _sentiment(ticker, market, name, asof, cfg, deps) -> dict[str, Section]:
    now = deps.now
    if market != "KR":
        flow = skipped_section("not available for US", now=now)
    elif deps.market is None:
        flow = skipped_section("no market client", now=now)
    else:
        flow = run_section(
            lambda: _dump(deps.market.get_investor_kr(ticker, days=cfg.kis.investor_days)),
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
            ),
            enabled=cfg.community.enabled,
            now=now,
        ),
        "trends": skipped_section("phase6", now=now),
        "manual_inputs": skipped_section("phase6", now=now),
    }


def _name(ticker: str, market: Market, deps: InputDeps) -> str:
    if market != "KR" or deps.dart is None:
        return ticker
    try:
        return deps.dart.corp_name(ticker) or ticker
    except Exception as e:  # noqa: BLE001 - 이름 조회 실패는 ticker 로 대체
        logger.warning("종목명 조회 실패 %s: %s", ticker, type(e).__name__)
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
    technical = _technical(ticker, market, exchange, cfg, deps)
    result: dict[str, Any] = {
        "meta": {
            "ticker": ticker,
            "market": market,
            "exchange": exchange,
            "name": name,
            "asof": asof.astimezone(UTC).isoformat(),
            "generated_at": deps.now().isoformat(),
            "schema_version": SCHEMA_VERSION,
        },
        "technical": technical,
        "fundamental": _fundamental(ticker, market, asof, cfg, deps, technical),
        "news_macro": _news_macro(ticker, market, name, asof, cfg, secrets, deps),
        "sentiment": _sentiment(ticker, market, name, asof, cfg, deps),
    }
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
    d = run_dir(asof.astimezone(_KST).date(), market, ticker, state)
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
