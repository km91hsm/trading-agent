"""FastAPI 앱 조립. 라우터는 `request.app.state.deps`(WebDeps) 로 의존성을 얻는다."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from trader.config import AccountConfig, Settings
from trader.notify.telegram import NullNotifier

NO_DIST_HTML = (
    "<!doctype html><html lang='ko'><meta charset='utf-8'><body style='font-family:sans-serif'>"
    "<h2>trader 웹 UI</h2><p>web/dist 가 없습니다. 프런트를 빌드하세요:</p>"
    "<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>"
    "<p>API 는 <a href='/api/system/health'>/api/system/health</a> 로 확인할 수 있습니다.</p>"
    "</body></html>"
)


@dataclass
class WebDeps:
    settings: Settings
    accounts: list[AccountConfig]
    conn_factory: Callable[[], sqlite3.Connection]
    brokers: dict[str, Any] = field(default_factory=dict)
    market: Any | None = None
    state: Path | None = None
    token: str | None = None
    dist: Path | None = None
    notifier: Any = field(default_factory=NullNotifier)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    extra_hosts: list[str] = field(default_factory=list)  # --host 로 다른 바인드를 쓸 때
    schedule: Any | None = None  # ScheduleConfig: /api/system/daemon 의 잡 예정표 (없으면 빈 목록)
    quote_hub: Any | None = None  # 국내 실시간 시세 허브(web/live_quotes.QuoteHub), 없으면 REST 만

    def account(self, account_id: str) -> AccountConfig | None:
        return next((a for a in self.accounts if a.id == account_id), None)


def create_app(deps: WebDeps) -> FastAPI:
    from trader.web import sse
    from trader.web.routers import (
        accounts,
        charts,
        control,
        features,
        jobs,
        ml,
        names,
        news,
        orders,
        performance,
        portfolio,
        quotes,
        rl,
        runs,
        system,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if deps.quote_hub is not None:  # 실시간 시세 허브의 KIS 웹소켓·스레드를 함께 내린다
            deps.quote_hub.stop()

    app = FastAPI(
        title="trader", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan
    )
    app.state.deps = deps
    # 폰 접속(Stock D-054): `tailscale serve` 가 tailnet 이름으로 넘겨 주는 요청은 허용 로그인만.
    # uvicorn 은 루프백에만 열고(tailscale serve 가 127.0.0.1 로 넘긴다), 제어 API 는 토큰 그대로
    ts_hosts = {h.lower() for h in deps.settings.web.tailscale_hosts}
    ts_users = {u.lower() for u in deps.settings.web.tailscale_users}

    @app.middleware("http")
    async def tailscale_gate(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host in ts_hosts:
            login = (request.headers.get("tailscale-user-login") or "").lower()
            client = request.client.host if request.client else ""
            if client not in ("127.0.0.1", "::1") or not login or login not in ts_users:
                return JSONResponse({"detail": "tailnet 허용 사용자가 아닙니다"}, status_code=403)
        return await call_next(request)

    # 로컬 전용: DNS 리바인딩으로 토큰 없는 조회 API 를 읽지 못하게 Host 를 제한한다
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "127.0.0.1",
            "localhost",
            "[::1]",
            "testserver",
            *deps.extra_hosts,
            *sorted(ts_hosts),
        ],
    )
    routers = (
        system.router,
        accounts.router,
        portfolio.router,
        portfolio.lots_router,
        orders.router,
        runs.router,
        names.router,
        quotes.router,
        charts.router,
        ml.router,
        features.router,
        news.router,
        performance.router,
        rl.router,
        control.public_router,
        control.router,
        jobs.public_router,
        jobs.router,
        sse.router,
    )
    for router in routers:
        app.include_router(router, prefix="/api")

    @app.get("/api/{rest:path}", include_in_schema=False)
    def _api_404(rest: str) -> JSONResponse:
        return JSONResponse({"detail": f"없는 API: /api/{rest}"}, status_code=404)

    dist = deps.dist.resolve() if deps.dist is not None else None
    if dist is not None and (dist / "index.html").is_file():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{rest:path}", include_in_schema=False)
        def _spa(rest: str, request: Request) -> FileResponse:
            candidate = (dist / rest) if rest else None
            if (
                candidate is not None
                and candidate.is_file()
                and dist in candidate.resolve().parents
            ):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    else:

        @app.get("/{rest:path}", include_in_schema=False)
        def _notice(rest: str) -> HTMLResponse:
            return HTMLResponse(NO_DIST_HTML)

    return app
