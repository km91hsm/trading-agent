"""프롬프트 직전 종목명·티커 익명화 (스펙 §3.3, settings.anonymize).

문자열 값만 치환하고 dict 키는 바꾸지 않는다. 티커·이름 모두 단어 경계(앞뒤가 영숫자가 아님)에서만
치환하므로 "10059300" 안의 "005930" 이나 "RISK" 안의 "SK" 는 건드리지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Alias:
    ticker: str
    name: str
    ticker_alias: str = "TICKER_X"
    name_alias: str = "종목X"


def _pattern(token: str) -> re.Pattern[str]:
    # 한글 이름은 \b 가 어긋나므로 "앞뒤가 영숫자가 아니다" 로 경계를 정의한다
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])")


def _walk(obj: Any, pairs: list[tuple[re.Pattern[str], str]]) -> Any:
    if isinstance(obj, str):
        out = obj
        for pat, repl in pairs:
            out = pat.sub(repl, out)
        return out
    if isinstance(obj, dict):
        return {k: _walk(v, pairs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, pairs) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, pairs) for v in obj)
    return obj


def _pairs(alias: Alias, *, forward: bool) -> list[tuple[re.Pattern[str], str]]:
    items = []
    if alias.name and alias.name != alias.ticker:
        items.append((alias.name, alias.name_alias))
    if alias.ticker:
        items.append((alias.ticker, alias.ticker_alias))
    if not forward:
        items = [(b, a) for a, b in items]
    # 긴 토큰부터 치환해 부분 겹침을 피한다
    items.sort(key=lambda p: len(p[0]), reverse=True)
    return [(_pattern(src), dst) for src, dst in items]


def anonymize(obj: Any, alias: Alias) -> Any:
    """복사본을 돌려준다. 원본은 바꾸지 않는다."""
    return _walk(obj, _pairs(alias, forward=True))


def deanonymize(obj: Any, alias: Alias) -> Any:
    return _walk(obj, _pairs(alias, forward=False))
