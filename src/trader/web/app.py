"""FastAPI 앱 조립. 라우터는 `request.app.state.deps`(WebDeps) 로 의존성을 얻는다."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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

    def account(self, account_id: str) -> AccountConfig | None:
        return next((a for a in self.accounts if a.id == account_id), None)


def create_app(deps: WebDeps) -> FastAPI:
    from trader.web.routers import control, system

    app = FastAPI(title="trader", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.deps = deps
    for router in (system.router, control.router):
        app.include_router(router, prefix="/api")

    @app.get("/api/{rest:path}", include_in_schema=False)
    def _api_404(rest: str) -> JSONResponse:
        return JSONResponse({"detail": f"없는 API: /api/{rest}"}, status_code=404)

    dist = deps.dist
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
