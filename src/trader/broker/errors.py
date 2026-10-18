"""KIS 오류 타입. 모든 KIS 실패는 KisApiError 하나로 통일한다."""

from __future__ import annotations

# 요청이 서버에 닿은 뒤 응답을 못 받은 실패(ReadTimeout 등). 주문이 접수됐을 수 있어
# NETWORK(보내지도 못함)와 구분하고, executor 는 이 주문을 failed 가 아닌 unknown 으로 남긴다.
UNKNOWN_RESPONSE_CODE = "NETWORK_UNKNOWN"


class KisApiError(Exception):
    """KIS 호출 실패.

    msg_cd 는 KIS 응답 코드이거나, KIS 응답이 아닌 실패의 합성 코드
    (AUTH / NETWORK / NETWORK_UNKNOWN / HTTP<status> / INVALID_JSON / UNSUPPORTED)다.
    메시지에 앱키·토큰을 넣지 않는다.
    """

    def __init__(self, msg_cd: str, msg1: str, tr_id: str | None = None) -> None:
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.tr_id = tr_id
        suffix = f" [{tr_id}]" if tr_id else ""
        super().__init__(f"{msg_cd}: {msg1}{suffix}")
