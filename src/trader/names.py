"""종목명: 원장 `ticker_names`(잔고·00_inputs 관측 시 upsert) → 캐시 JSON(DART 기업코드 맵·EDGAR
티커 맵, `fetched_at` 무시) → 코드만. 어떤 경로도 네트워크를 타지 않는다. 표기는 `label` 하나다."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from trader.ledger import repo
from trader.platform import paths

logger = logging.getLogger("trader.names")

_CACHE_FILES = (("KR", "dart_corpcode.json", "corp_name"), ("US", "edgar_tickers.json", "name"))


def default_cache_dir() -> Path:
    """수집기 캐시 디렉터리 (`cli._build_dart/_build_edgar` 와 같은 곳)."""
    return paths.state_dir() / "cache"


def load_cache_names(cache_dir: Path) -> dict[tuple[str, str], str]:
    """캐시 JSON 두 개를 직접 읽는다. 만료된 캐시도 이름은 유효하다. 없음·깨짐은 빈 dict."""
    out: dict[tuple[str, str], str] = {}
    for market, fname, key in _CACHE_FILES:
        p = cache_dir / fname
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            table = data.get("map") or {}
        except (OSError, ValueError, AttributeError) as e:
            logger.warning("종목명 캐시를 읽지 못함 %s: %s", p.name, e)
            continue
        for ticker, row in table.items():
            name = str((row or {}).get(key) or "").strip() if isinstance(row, dict) else ""
            if name and name != ticker:
                out[(market, str(ticker))] = name
    return out


def resolve(
    conn: sqlite3.Connection | None, cache_dir: Path, market: str, ticker: str
) -> str | None:
    """원장 → 캐시(적중하면 원장에 source='cache' 로 적재) → None."""
    if conn is not None:
        found = repo.ticker_names(conn, market=market).get((market, ticker))
        if found:
            return found
    cached = load_cache_names(cache_dir).get((market, ticker))
    if cached and conn is not None:
        repo.upsert_ticker_name(conn, market, ticker, cached, "cache")
    return cached


def resolve_many(
    conn: sqlite3.Connection | None, cache_dir: Path, keys: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """여러 종목을 한 번에: 원장 전체 1회 + 캐시 파일 1회."""
    out: dict[tuple[str, str], str] = {}
    ledger = repo.ticker_names(conn) if conn is not None else {}
    cache: dict[tuple[str, str], str] | None = None
    for key in keys:
        if key in ledger:
            out[key] = ledger[key]
            continue
        if cache is None:
            cache = load_cache_names(cache_dir)
        name = cache.get(key)
        if name:
            out[key] = name
            if conn is not None:
                repo.upsert_ticker_name(conn, key[0], key[1], name, "cache")
    return out


def label(ticker: str, name: str | None) -> str:
    """사람이 읽는 표기: '삼성전자 005930', 이름이 없으면 '005930'."""
    clean = (name or "").strip()
    return f"{clean} {ticker}" if clean and clean != ticker else ticker


def display(conn: sqlite3.Connection | None, cache_dir: Path, market: str, ticker: str) -> str:
    return label(ticker, resolve(conn, cache_dir, market, ticker))
