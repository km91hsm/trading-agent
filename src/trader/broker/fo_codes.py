"""국내선물옵션 종목코드 정규화(Phase 26).

체결·잔고 응답의 `pdno` 는 표준코드(12자리)로 올 수 있고 주문·시세는 단축코드(선물 6·옵션 9자리)를
쓴다. 표준↔단축 대응은 마스터(`fo_master.Contract.std_code`)가 출처이고, 대응을 모르면 그대로 둔다
(실기 1 에서 확정). 종류는 코드 접두(상품 설정)로만 판정한다 — 방향의 근거가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from trader.broker.models import FoKind

if TYPE_CHECKING:
    from trader.broker.fo_master import Contract
    from trader.config import FoProductCfg


def normalize(code: str, contracts: Iterable[Contract] | None = None) -> str:
    """공백 제거·대문자. 12자리 표준코드는 마스터로 단축코드로 바꾸고, 못 찾으면 그대로."""
    c = str(code or "").strip().upper()
    if len(c) == 12 and contracts is not None:
        for k in contracts:
            if k.std_code.upper() == c:
                return k.code
    return c


def kind_of(code: str, product: FoProductCfg) -> FoKind | None:
    """상품 설정의 접두로 종류 판정. 다른 상품·위클리·미지 접두는 None."""
    c = str(code or "").strip().upper()
    if c.startswith(product.futures_prefix):
        return "FUTURES"
    if c.startswith(product.call_prefix):
        return "CALL"
    if c.startswith(product.put_prefix):
        return "PUT"
    return None
