"""`trader universe sectors`: KIS 업종으로 sectors.yaml 의 종목→섹터 항목을 제안·기록한다.

결정적 데이터 원칙: 업종→대분류 표는 사람이 관리하고, 명령은 그 표로 바꿔 **빠진 종목만** 제안한다.
`apply` 는 `--write` 때만 yaml 의 `kr:`/`us:` 블록 끝에 줄을 덧붙이며 기존 줄·주석은 그대로 둔다.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from trader.config import SectorMap, Universe, load_sectors

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
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == f"{key}:"), None)
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
        # `kr: {}` 같은 인라인 꼴이면 블록 꼴로 바꾼다
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
        if idx is None:
            lines.append(f"{key}:")
            idx = len(lines) - 1
        else:
            lines[idx] = f"{key}:"
        insert_at = idx + 1
    else:
        insert_at = pos[1]
    stamp = f"(auto {today.isoformat()})"
    new = [
        f'  "{r["ticker"]}": {r["suggested"]}  # {r.get("industry") or "-"} {stamp}' for r in adds
    ]
    lines[insert_at:insert_at] = new
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_sectors(path)  # 쓴 결과가 읽히는지 바로 확인 (깨졌으면 ConfigError)
    logger.info("sectors.yaml %s 에 %d종목 추가: %s", key, len(adds), [r["ticker"] for r in adds])
    return len(adds)
