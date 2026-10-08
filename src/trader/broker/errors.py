"""KIS 오류 타입. 모든 KIS 실패는 KisApiError 하나로 통일한다."""

from __future__ import annotations


class KisApiError(Exception):
    """KIS 호출 실패.

    msg_cd 는 KIS 응답 코드이거나, KIS 응답이 아닌 실패의 합성 코드
    (AUTH / NETWORK / HTTP<status> / INVALID_JSON / UNSUPPORTED)다.
    메시지에 앱키·토큰을 넣지 않는다.
    """

    def __init__(self, msg_cd: str, msg1: str, tr_id: str | None = None) -> None:
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.tr_id = tr_id
        suffix = f" [{tr_id}]" if tr_id else ""
        super().__init__(f"{msg_cd}: {msg1}{suffix}")
