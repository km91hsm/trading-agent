"""제어 API 토큰 의존성. 토큰 미설정이면 제어 API 는 닫힌다 (스펙 §8)."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


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
