"""SSE `/api/events`: 원장 카운터 스냅샷이 바뀔 때만 `change` 이벤트, 15초마다 ping.

비동기 제너레이터라 스레드 풀을 점유하지 않는다(스냅샷 조회만 run_in_threadpool).
트리거·데몬 이벤트 푸시는 Phase 6 (지금은 폴링 기반).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from trader.control import control

router = APIRouter(tags=["events"])

PING_EVERY_S = 15.0


def snapshot(conn: sqlite3.Connection) -> dict:
    def _max(table: str) -> int:
        row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}").fetchone()
        return int(row["m"])

    pending = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'").fetchone()[
        "n"
    ]
    return {
        "orders": _max("orders"),
        "approvals_pending": int(pending),
        "approvals": _max("approvals"),
        "runs": _max("runs"),
        # agent_calls 는 분석 세션 중 서브에이전트 호출마다 늘어 change 가 쏟아지므로 넣지 않는다
        "decisions": _max("decisions"),
        "triggers": _max("triggers"),
        "watchlist": ",".join(control.get_watchlist(conn)),
        "paused": control.is_paused(conn),
    }


async def stream(
    deps, *, interval_s: float = 2.0, max_events: int | None = None
) -> AsyncIterator[str]:
    conn = deps.conn_factory()  # 스트림당 연결 1개 (틱마다 열지 않는다; check_same_thread=False)
    try:
        async for item in _ticks(conn, interval_s, max_events):
            yield item
    finally:
        conn.close()


async def _ticks(conn, interval_s: float, max_events: int | None) -> AsyncIterator[str]:
    last: dict | None = None
    sent = 0
    last_ping = time.monotonic()
    while True:
        snap = await run_in_threadpool(snapshot, conn)
        if snap != last:
            last = snap
            sent += 1
            yield f"event: change\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            if max_events is not None and sent >= max_events:
                return
        elif time.monotonic() - last_ping >= PING_EVERY_S:
            last_ping = time.monotonic()
            yield ": ping\n\n"
        await anyio.sleep(interval_s)


@router.get("/events")
async def events(request: Request, once: int = 0) -> StreamingResponse:
    deps = request.app.state.deps
    gen = stream(deps, max_events=1 if once else None)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
