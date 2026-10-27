"""제어 API 토큰 의존성. 토큰 미설정이면 제어 API 는 닫힌다 (스펙 §8)."""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

PROXY_HEADERS = ("tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip")


def is_direct_local(request: Request) -> bool:
    """이 PC 의 브라우저가 직접 연 요청인가: 루프백 클라이언트 ∧ 프록시 흔적 없음 ∧ Host 가
    루프백(tailscale serve 로 들어온 폰 요청은 아니다). 키 입력·토큰 받기는 이때만."""
    client = request.client.host if request.client else ""
    try:  # URL 파서로 호스트·포트를 가른다 — `[::1]`(포트 없음)을 rsplit 이 `:` 로 만들던 것(2-W8)
        host = (urlsplit("//" + (request.headers.get("host") or "")).hostname or "").lower()
    except ValueError:
        host = ""
    return (
        client in ("127.0.0.1", "::1")
        and host in ("127.0.0.1", "localhost", "::1")
        and not any(request.headers.get(h) for h in PROXY_HEADERS)
    )


def require_direct_local(request: Request) -> None:
    if not is_direct_local(request):
        raise HTTPException(status_code=403, detail="이 PC 에서 직접 연 화면에서만 할 수 있습니다")


def require_token(request: Request) -> None:
    token = request.app.state.deps.token
    header = request.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    if (
        not token
        or not given
        or not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=401, detail="토큰이 필요합니다 (Authorization: Bearer <WEB_TOKEN>)"
        )
