"""사용자 수동 입력 (스펙 §3): state/manual_inputs/<ticker>.md | .json (종목별) 과
`_market_<KR|US>.md` (시장 공통, 웹 '읽은 기사 붙여 넣기' 폼이 덧붙인다). 수정 시각이 received_at.

시장 공통 파일은 `## <ISO 시각> | <제목> | <URL>` 머리줄 + 메모 본문 블록의 나열이며,
읽을 때 asof 이후 항목을 뺀다(룩어헤드)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from trader.platform import paths

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
_MARKETS = ("KR", "US")
MAX_MARKET_NOTES = 20
_HEADER_RE = re.compile(r"^## (?P<ts>\S+) \| (?P<title>.*?) \| (?P<url>\S*)$")


def _dir(state: Path | None) -> Path:
    base = state if state is not None else paths.state_dir()
    return base / "manual_inputs"


def read(ticker: str, *, state: Path | None = None) -> dict | None:
    """없으면 None. md → {format, text}, json → {format, data}, 파싱 실패 → {error}."""
    if not _TICKER_RE.match(ticker or ""):
        return None
    d = _dir(state)
    for ext in ("md", "json"):
        p = d / f"{ticker}.{ext}"
        if not p.is_file():
            continue
        updated = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"format": ext, "path": str(p), "updated_at": updated, "error": str(e)}
        if ext == "md":
            return {"format": "md", "path": str(p), "updated_at": updated, "text": text}
        try:
            return {
                "format": "json",
                "path": str(p),
                "updated_at": updated,
                "data": json.loads(text),
            }
        except ValueError as e:
            return {
                "format": "json",
                "path": str(p),
                "updated_at": updated,
                "error": f"JSON 오류: {e}",
            }
    return None


def _market_path(market: str, state: Path | None) -> Path:
    if market not in _MARKETS:
        raise ValueError(f"시장은 KR 또는 US: {market}")
    return _dir(state) / f"_market_{market}.md"


def _clean_line(s: str) -> str:
    return " ".join((s or "").replace("|", "/").split())


def append_market(
    market: str,
    *,
    title: str,
    url: str = "",
    note: str = "",
    now: datetime | None = None,
    state: Path | None = None,
) -> dict:
    """시장 공통 파일에 항목 하나를 덧붙인다(파일·디렉터리는 없으면 만든다). 반환은 저장된 항목."""
    title = _clean_line(title)
    if not title:
        raise ValueError("제목이 필요합니다")
    url = _clean_line(url)
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("URL 은 http(s):// 로 시작해야 합니다")
    ts = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    body = (note or "").strip().replace("\n## ", "\n ## ")[:4000]
    p = _market_path(market, state)
    p.parent.mkdir(parents=True, exist_ok=True)
    block = f"## {ts} | {title} | {url}\n{body}\n\n" if body else f"## {ts} | {title} | {url}\n\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(block)
    return {"market": market, "received_at": ts, "title": title, "url": url, "note": body}


def read_market(
    market: str, *, asof: datetime | None = None, state: Path | None = None
) -> list[dict]:
    """시장 공통 항목(최신 순, 최대 MAX_MARKET_NOTES). asof 이후에 적힌 항목은 뺀다."""
    if market not in _MARKETS:
        return []
    p = _dir(state) / f"_market_{market}.md"
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    cutoff = asof.astimezone(UTC).isoformat() if asof is not None else None
    items: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            cur = {
                "received_at": m.group("ts"),
                "title": m.group("title"),
                "url": m.group("url"),
                "note": "",
            }
            items.append(cur)
        elif cur is not None and line.strip():
            cur["note"] = (cur["note"] + "\n" + line).strip()
    kept = [i for i in items if cutoff is None or i["received_at"] <= cutoff]
    kept.sort(key=lambda i: i["received_at"], reverse=True)
    return kept[:MAX_MARKET_NOTES]
