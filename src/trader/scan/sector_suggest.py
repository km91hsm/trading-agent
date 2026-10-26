"""`trader universe sectors`: KIS 업종으로 sectors.yaml 의 종목→섹터 항목을 제안·기록한다.

결정적 데이터 원칙: 업종→대분류 표는 사람이 관리하고, 명령은 그 표로 바꿔 **빠진 종목만** 제안한다.
`apply` 는 `--write` 때만 yaml 의 `kr:`/`us:` 블록 끝에 줄을 덧붙이며 기존 줄·주석은 그대로 둔다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from trader.config import ConfigError, SectorMap, Universe, load_sectors
from trader.platform import paths
from trader.platform.fsutil import atomic_replace

logger = logging.getLogger("trader.scan")


def suggest(
    universe: Universe,
    sectors: SectorMap,
    market_client: Any,
    *,
    market: str,
    only_missing: bool = True,
) -> list[dict[str, Any]]:
    """종목마다 {ticker, exchange, industry, current, suggested, note}. 조회 실패·표에 없는 업종은
    suggested None 과 note 로 알린다 (추측하지 않음)."""
    if market == "KR":
        ids: list[tuple[str, str | None]] = [(t, None) for t in universe.kr]
    else:
        ids = [(u.symbol, u.exchange) for u in universe.us]
    table_name = f"{market.lower()}_industry_to_sector"
    out: list[dict[str, Any]] = []
    for ticker, exchange in ids:
        current = sectors.sector_of(ticker, market)  # type: ignore[arg-type]
        if only_missing and current is not None:
            continue
        industry: str | None = None
        note = ""
        try:
            industry = (
                market_client.get_industry_kr(ticker)
                if market == "KR"
                else market_client.get_industry_us(ticker, exchange)
            )
        except Exception as e:  # noqa: BLE001 - 종목 단위 격리, 사유를 표에 남긴다
            note = f"조회 실패: {type(e).__name__}: {e}"
        suggested = sectors.classify(industry, market)  # type: ignore[arg-type]
        if not note and industry is None:
            note = "KIS 가 업종을 주지 않음"
        elif not note and suggested is None:
            note = f"업종 매핑 없음: {industry} → {table_name} 에 추가"
        out.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "industry": industry,
                "current": current,
                "suggested": suggested,
                "note": note,
            }
        )
    return out


def _block_end(lines: list[str], key: str) -> tuple[int, int] | None:
    """`key:` 블록의 (헤더 줄 번호, 마지막 항목 줄 다음 번호). 블록이 없으면 None."""
    start = next(
        (i for i, ln in enumerate(lines) if ln.split("#", 1)[0].rstrip() == f"{key}:"), None
    )
    if start is None:
        return None
    end = start + 1
    last = start
    while end < len(lines):
        ln = lines[end]
        if ln.strip() == "" or ln.startswith("  "):
            if ln.strip():
                last = end
            end += 1
            continue
        break
    return start, last + 1


def apply(path: Path, market: str, rows: list[dict[str, Any]], *, today: date) -> int:
    """제안이 있고 아직 없는 종목만 `kr:`/`us:` 블록 끝에 덧붙인다. 반환 추가 수. 멱등."""
    key = market.lower()
    existing = load_sectors(path)
    have = existing.kr if market == "KR" else existing.us
    adds = [
        r
        for r in rows
        if r.get("suggested") and r.get("current") is None and r["ticker"] not in have
    ]
    if not adds:
        return 0
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    pos = _block_end(lines, key)
    if pos is None:
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
        if idx is None:
            lines.append(f"{key}:")
            idx = len(lines) - 1
        else:
            body, _, comment = lines[idx].partition("#")
            inline = body.split(":", 1)[1].strip()
            if inline not in ("", "{}"):
                # 값이 든 인라인 매핑을 블록으로 바꾸면 기존 항목이 지워진다
                # → 사람이 블록 꼴로 옮긴 뒤 다시
                raise ConfigError(
                    f"{path.name} 의 `{key}:` 가 인라인 매핑입니다. "
                    "블록 꼴로 바꾼 뒤 다시 실행하세요"
                )
            lines[idx] = f"{key}:" + (f"  #{comment}" if comment else "")
        insert_at = idx + 1
    else:
        insert_at = pos[1]
    stamp = f"(auto {today.isoformat()})"
    new = [
        f"  {json.dumps(r['ticker'], ensure_ascii=False)}: {r['suggested']}"
        f"  # {r.get('industry') or '-'} {stamp}"
        for r in adds
    ]
    lines[insert_at:insert_at] = new
    new_text = "\n".join(lines) + "\n"
    try:  # 쓰기 전에 검증 — 깨진 설정을 디스크에 남기지 않는다
        SectorMap.model_validate(yaml.safe_load(new_text) or {})
    except Exception as e:  # noqa: BLE001 - yaml 오류·검증 오류 모두
        raise ConfigError(f"{path.name} 갱신 결과가 유효하지 않아 쓰지 않았습니다: {e}") from e
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    logger.info("sectors.yaml %s 에 %d종목 추가: %s", key, len(adds), [r["ticker"] for r in adds])
    return len(adds)


def sync(src: Path, dst: Path, market: str, *, today: date) -> int:
    """src(config) 에 있고 dst(templates) 에 빠진 종목을 src 값으로 덧붙인다 — 패리티 유지.
    KIS 제안이 아니라 config 의 (수동) 값을 복사한다. 반환 추가 수. 멱등."""
    src_map = load_sectors(src)
    dst_map = load_sectors(dst)
    a = src_map.kr if market == "KR" else src_map.us
    b = dst_map.kr if market == "KR" else dst_map.us
    rows = [
        {"ticker": t, "industry": "config 동기화", "current": None, "suggested": sec}
        for t, sec in a.items()
        if t not in b
    ]
    return apply(dst, market, rows, today=today) if rows else 0


# --- 자동 분류 캐시 (웹 섹터 비중) --------------------------------------------------------------
# sectors.yaml 에 없는 **보유·관심** 종목(장중 발굴로 유니버스 밖에서 산 종목 등)이 섹터 비중에서
# 전부 '기타' 로 묶이던 문제. 잔고를 관측할 때(데몬 잔고 갱신·웹 잔고 새로고침) 빠진 종목만 KIS
# 업종명 → 같은 표(`*_industry_to_sector`)로 대분류해 `state/cache/sector_auto.json` 에 남긴다.
# yaml 이 언제나 우선이고(사람이 관리), 표에 없는 업종은 추측하지 않는다(sector None 으로 캐시해
# 다시 조회하지 않음). 조회 실패는 캐시하지 않아 다음 관측 때 다시 시도한다.
AUTO_FILE = "sector_auto.json"
# KIS 가 업종을 주지 않는 ETF·ETN 은 종목명의 운용사 상표로만 'ETF' 로 묶는다(표시 전용 — 리스크
# 게이트는 sectors.yaml 만 본다). 상표 목록은 국내 상장 ETF 브랜드.
ETF_SECTOR = "ETF"
_ETF_BRANDS = re.compile(
    r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|HANARO|ARIRANG|KOSEF|PLUS|KIWOOM|TIMEFOLIO|WON|"
    r"BNK|UNICORN|파워|마이티|히어로즈|1Q|에셋플러스|TRUSTON|FOCUS|DAISHIN343)\b",
    re.I,
)


def _etf_by_name(name: str | None) -> bool:
    return bool(name and (_ETF_BRANDS.search(name.strip()) or " ETN" in name or "ETF" in name))


def _auto_path(state: Path | None) -> Path:
    return (state or paths.state_dir()) / "cache" / AUTO_FILE


def load_auto(state: Path | None = None) -> dict[str, dict[str, dict]]:
    p = _auto_path(state)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def auto_classify(
    sectors: SectorMap,
    market_client: Any,
    *,
    market: str,
    tickers: Iterable[str],
    state: Path | None = None,
    exchanges: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> dict[str, str]:
    """yaml·캐시에 없는 종목만 업종을 조회해 캐시에 적는다. 새로 대분류된 {종목: 섹터}.
    업종 표에 없는데 이름이 ETF 상표면(`names`) 'ETF' 로 묶는다."""
    cache = load_auto(state)
    seen = cache.setdefault(market, {})
    got: dict[str, str] = {}
    changed = False
    for t in dict.fromkeys(str(x) for x in tickers):
        if sectors.sector_of(t, market) is not None or t in seen:  # type: ignore[arg-type]
            continue
        try:
            industry = (
                market_client.get_industry_kr(t)
                if market == "KR"
                else market_client.get_industry_us(t, (exchanges or {}).get(t) or "NAS")
            )
        except Exception as e:  # noqa: BLE001 - 종목 단위 격리, 다음 관측 때 다시
            logger.info("업종 조회 실패 %s %s: %s", market, t, e)
            continue
        sector = sectors.classify(industry, market)  # type: ignore[arg-type]
        if sector is None and _etf_by_name((names or {}).get(t)):
            sector = ETF_SECTOR
        seen[t] = {"sector": sector, "industry": industry, "at": datetime.now(UTC).isoformat()}
        changed = True
        if sector:
            got[t] = sector
    if changed:
        p = _auto_path(state)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        atomic_replace(tmp, p)
    return got


class SectorResolver:
    """종목 → 섹터: yaml 우선, 없으면 자동 캐시. 자동으로 정해진 종목은 `auto_tickers` 에 모아
    화면이 '자동' 꼬리표를 붙이게 한다(views.sector_weights 가 읽는다)."""

    def __init__(self, sectors: SectorMap | None, market: str, auto: dict[str, dict]) -> None:
        self.sectors, self.market, self.auto = sectors, market, auto
        self.auto_tickers: set[str] = set()

    def __call__(self, ticker: str) -> str | None:
        t = str(ticker)
        if self.sectors is not None:
            s = self.sectors.sector_of(t, self.market)  # type: ignore[arg-type]
            if s:
                return s
        s = (self.auto.get(t) or {}).get("sector")
        if s:
            self.auto_tickers.add(t)
        return s


def resolver(
    sectors: SectorMap | None, market: str, *, state: Path | None = None
) -> SectorResolver:
    return SectorResolver(sectors, market, load_auto(state).get(market, {}))
