"""종목명: 원장 `ticker_names`(잔고·00_inputs 관측 시 upsert) → 캐시 JSON(DART 기업코드 맵·EDGAR
티커 맵, `fetched_at` 무시) → 설정 `instrument_names`(헤지·현금·지수 대용 ETF) → 코드만. 어떤
경로도 네트워크를 타지 않는다. 표기는 `label` 하나다."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import date
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


_CONFIG_MEMO: dict[tuple[str, float], dict[tuple[str, str], str]] = {}


def load_config_names() -> dict[tuple[str, str], str]:
    """설정 `instrument_names` → {(시장, 코드): 이름}. 설정 파일 mtime 으로 메모하고 읽기 실패는
    빈 dict(기본값은 설정 모델에 있어 파일에 없어도 채워진다)."""
    from trader import config

    p = paths.config_dir() / "settings.yaml"
    try:
        key = (str(p), p.stat().st_mtime if p.exists() else 0.0)
        if key not in _CONFIG_MEMO:
            s = config.load_settings(p) if p.exists() else config.Settings()
            _CONFIG_MEMO.clear()
            _CONFIG_MEMO[key] = {
                (m, t): n for m, table in s.instrument_names.items() for t, n in table.items() if n
            }
        return _CONFIG_MEMO[key]
    except Exception as e:  # noqa: BLE001 — 이름은 보조 정보
        logger.debug("설정 종목명을 읽지 못함: %s", e)
        return {}


def resolve(
    conn: sqlite3.Connection | None, cache_dir: Path, market: str, ticker: str
) -> str | None:
    """원장 → 캐시 → 설정 운용 상품 이름(적중하면 원장에 source='cache'|'config' 로 적재) → None."""
    if conn is not None:
        found = repo.ticker_names(conn, market=market).get((market, ticker))
        if found:
            return found
    name, source = load_cache_names(cache_dir).get((market, ticker)), "cache"
    if not name:
        name, source = load_config_names().get((market, ticker)), "config"
    if name and conn is not None:
        repo.upsert_ticker_name(conn, market, ticker, name, source)
    return name


def resolve_many(
    conn: sqlite3.Connection | None, cache_dir: Path, keys: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """여러 종목을 한 번에: 원장 전체 1회 + 캐시 파일 1회 + 설정 이름(메모)."""
    out: dict[tuple[str, str], str] = {}
    ledger = repo.ticker_names(conn) if conn is not None else {}
    cache: dict[tuple[str, str], str] | None = None
    for key in keys:
        if key in ledger:
            out[key] = ledger[key]
            continue
        if cache is None:
            cache = load_cache_names(cache_dir)
        name, source = cache.get(key), "cache"
        if not name:
            name, source = load_config_names().get(key), "config"
        if name:
            out[key] = name
            if conn is not None:
                repo.upsert_ticker_name(conn, key[0], key[1], name, source)
    return out


MISS_FLAG = "names_miss"  # {"<시장>:<종목>": "YYYY-MM-DD"} — 그날 KIS 가 이름을 못 준 종목


def fill_missing(
    conn: sqlite3.Connection,
    lookup: Callable[..., str | None],
    *,
    cache_dir: Path,
    today: date,
    limit: int = 20,
) -> dict:
    """데몬 names_fill: 원장에 나왔지만 이름이 없는 종목을 캐시·설정 → `lookup`(KIS 종목 기본정보)
    순으로 채운다(source='kis'). 못 찾은 종목은 그날 다시 묻지 않고, 한 번에 `limit` 건까지만
    조회한다. 화면 표시 경로는 이 함수를 부르지 않는다(네트워크 없음 유지)."""
    from trader.control import control

    keys = repo.unnamed_tickers(conn)
    local = resolve_many(conn, cache_dir, keys)  # 캐시·설정으로 채워지는 것은 조회하지 않는다
    missing = [k for k in keys if k not in local]
    misses = dict(control.get_flag(conn, MISS_FLAG) or {})
    misses = {k: v for k, v in misses.items() if v == today.isoformat()}  # 지난 날 기록은 버린다
    exchanges = {m: repo.ticker_exchanges(conn, m) for m in ("US",)}
    asked = filled = 0
    for market, ticker in missing:
        tag = f"{market}:{ticker}"
        if tag in misses:
            continue
        if asked >= limit:
            break
        asked += 1
        try:
            name = lookup(market, ticker, exchange=exchanges.get(market, {}).get(ticker))
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 나머지를 막지 않는다
            logger.warning("종목명 조회 실패 %s: %s", tag, e)
            name = None
        if name and repo.upsert_ticker_name(conn, market, ticker, name, "kis"):
            filled += 1
        else:
            misses[tag] = today.isoformat()
    control.set_flag(conn, MISS_FLAG, misses)
    return {"candidates": len(keys), "local": len(local), "asked": asked, "filled": filled}


def label(ticker: str, name: str | None) -> str:
    """사람이 읽는 표기: '삼성전자(005930)', 이름이 없으면 '005930' (Phase 24, 텔레그램·STATUS)."""
    clean = (name or "").strip()
    return f"{clean}({ticker})" if clean and clean != ticker else ticker


def display(conn: sqlite3.Connection | None, cache_dir: Path, market: str, ticker: str) -> str:
    """표시용 라벨. 이름은 보조 정보라 원장 잠금·캐시 오류가 호출자(알림·로그)를 막지 않는다."""
    try:
        return label(ticker, resolve(conn, cache_dir, market, ticker))
    except Exception as e:  # noqa: BLE001
        logger.debug("종목명 조회 실패 %s %s: %s", market, ticker, e)
        return label(ticker, None)


_SOURCE_RANK = {"held": 0, "watch": 1, "universe": 2, "ledger": 3, "listed": 4}


def _norm(s: str) -> str:
    return "".join(s.split()).casefold()


def search(
    conn: sqlite3.Connection | None,
    cache_dir: Path,
    q: str,
    *,
    market: str | None = None,
    universe: dict[tuple[str, str], str | None] | None = None,
    held: set[tuple[str, str]] | None = None,
    watch: set[tuple[str, str]] | None = None,
    limit: int = 20,
) -> list[dict]:
    """종목 코드·이름 부분 일치 검색 (Phase 24). 대상은 보유·관심·유니버스·원장 이름·캐시
    (상장 전체). 순서: 코드 접두 > 이름 접두 > 이름 부분 > 코드 부분, 같으면 보유 > 관심 >
    유니버스 > 원장 > 상장. 네트워크 없음. 빈 질의는 빈 목록."""
    nq = _norm(q)
    if not nq:
        return []
    universe = universe or {}
    held, watch = held or set(), watch or set()
    ledger = repo.ticker_names(conn) if conn is not None else {}
    names_map: dict[tuple[str, str], str] = {**load_cache_names(cache_dir), **ledger}
    keys = set(names_map) | set(universe) | held | watch
    scored = []
    for m, t in keys:
        if market and m != market:
            continue
        name = names_map.get((m, t)) or ""
        nt, nn = t.casefold(), _norm(name)
        if nt.startswith(nq):
            rank = 0
        elif nn.startswith(nq):
            rank = 1
        elif nq in nn:
            rank = 2
        elif nq in nt:
            rank = 3
        else:
            continue
        if (m, t) in held:
            source = "held"
        elif (m, t) in watch:
            source = "watch"
        elif (m, t) in universe:
            source = "universe"
        elif (m, t) in ledger:
            source = "ledger"
        else:
            source = "listed"
        scored.append((rank, _SOURCE_RANK[source], len(name), t, m, name, source))
    scored.sort()
    return [
        {
            "market": m,
            "ticker": t,
            "name": name or None,
            "exchange": universe.get((m, t)),
            "source": src,
        }
        for _, _, _, t, m, name, src in scored[:limit]
    ]
