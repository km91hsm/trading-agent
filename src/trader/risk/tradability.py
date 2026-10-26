"""매매 불가·경고 종목(Stock D-030 교훈): 관리종목·투자위험·투자경고·거래정지·단기과열·임시정지
종목은 신규 매수하지 않는다. 값은 국내 현재가 응답(`iscd_stat_cls_code` 종목상태,
`mrkt_warn_cls_code` 시장경고, `temp_stop_yn` 임시정지)을 `Quote` 가 싣고 `00_inputs` 로 온다.
US·코드가 없는 응답은 판정하지 않는다(None)."""

from __future__ import annotations

from typing import Any

STATUS = {
    "51": "관리종목",
    "52": "투자위험",
    "53": "투자경고",
    "58": "거래정지",
    "59": "단기과열",
}
WARN = {"02": "투자경고", "03": "투자위험"}


def block_reason(quote: Any) -> str | None:
    """매수 금지 사유 한 줄, 없으면 None. `quote` 는 dict(00_inputs) 또는 `Quote` 객체."""
    if quote is not None and not isinstance(quote, dict):
        quote = {k: getattr(quote, k, None) for k in ("status_code", "warn_code", "halted")}
    if not isinstance(quote, dict):
        return None
    found: list[str] = []
    status = str(quote.get("status_code") or "").strip()
    if status in STATUS:
        found.append(STATUS[status])
    warn = str(quote.get("warn_code") or "").strip()
    if warn in WARN and WARN[warn] not in found:
        found.append(WARN[warn])
    if quote.get("halted") is True:
        found.append("임시정지")
    return " · ".join(found) if found else None
