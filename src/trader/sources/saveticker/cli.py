"""SaveTicker 전용 CLI. 브로커·LLM·거래 데몬을 실행하지 않는다."""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import typer

from trader.config import KST, ConfigError
from trader.platform import paths
from trader.sources.saveticker.dom import months_between
from trader.sources.saveticker.models import Capture
from trader.sources.saveticker.service import View, build_views, choose_due, collect_cycle, lane_for
from trader.sources.saveticker.store import Store, atomic_json

app = typer.Typer(help="SaveTicker 브라우저 수집·파일 조회", no_args_is_help=True)


def root() -> Path:
    return paths.state_dir() / "external" / "saveticker" / "data"


def runtime() -> Path:
    return paths.state_dir() / "external" / "saveticker" / "runtime"


def sqlite_diagnostics(exc) -> dict:
    import sqlite3

    if isinstance(exc, sqlite3.Error):
        return {key: getattr(exc, key, None) for key in ("sqlite_errorcode", "sqlite_errorname")}
    return {}


def _print(value):
    typer.echo(json.dumps(value, ensure_ascii=False))


@app.command("status")
def status_cmd():
    """저장 상태와 실행 프로세스의 생존·최신 수집 시각을 표시한다."""
    from trader.platform.procs import pid_alive

    folder = root()
    p = folder / "status.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"streams": {}}
    runner = runtime() / "runner.json"
    if runner.exists():
        running = json.loads(runner.read_text(encoding="utf-8"))
        running["process_alive"] = pid_alive(running.get("pid", 0))
        data["runner"] = running
    now = datetime.now(UTC)
    for stream in data["streams"].values():
        last = stream.get("last_success_at")
        stream["age_s"] = (now - datetime.fromisoformat(last)).total_seconds() if last else None
    _print(data)


@app.command("import-capture")
def import_cmd(path: Path):
    """브라우저 캡처 JSON을 검증해 저장한다(원문 오류 값은 출력하지 않음)."""
    try:
        capture = Capture.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        typer.echo("수집 JSON 형식 또는 파일을 확인하세요", err=True)
        raise typer.Exit(1) from None
    _print(Store(root()).save(capture))


@app.command("query")
def query_cmd(
    asof: str | None = typer.Option(None, help="시간대가 포함된 ISO 시각"),
    ticker: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1, max=10000),
):
    """asof 이전에 실제 수신한 버전만 반환한다(미래 일정은 예정 정보로 유지)."""
    try:
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            raise ValueError
    except ValueError:
        typer.echo("asof에는 시간대가 포함된 ISO 시각이 필요합니다", err=True)
        raise typer.Exit(1) from None
    if not (root() / "feed.sqlite3").exists():
        _print([])
        return
    _print(Store(root()).query(asof=when, ticker=ticker, limit=limit))


@app.command("export")
def export_cmd():
    """SQLite에서 JSONL·최신 파일·상태를 재생성한다."""
    Store(root()).export()
    _print({"ok": True, "root": str(root())})


def _month_offset(now: datetime, offset: int) -> str:
    index = now.year * 12 + now.month - 1 + offset
    return f"{index // 12:04}-{index % 12 + 1:02}"


@app.command("run")
def run_cmd(
    once: bool = typer.Option(False, help="전체 스트림을 1회 수집한 뒤 종료"),
    headed: bool = typer.Option(True, "--headed/--headless", help="전용 브라우저 창 표시"),
    start_month: str | None = typer.Option(None, help="캘린더 시작 YYYY-MM"),
    end_month: str | None = typer.Option(None, help="캘린더 끝 YYYY-MM"),
    only: str | None = typer.Option(None, help="수집할 스트림 이름(쉼표로 구분, 생략 시 전체)"),
    max_scrolls: int = typer.Option(20, min=1, max=500),
    max_details: int = typer.Option(20, min=0, max=500),
    lane: str | None = typer.Option(None, hidden=True),
):
    """계정 파일로 자동 로그인해 수집한다. Ctrl+C로 종료한다."""
    from trader.sources.saveticker.browser import Browser, credentials

    if lane is not None and lane not in {"news", "community", "market", "background"}:
        typer.echo("알 수 없는 수집 작업입니다", err=True)
        raise typer.Exit(1)

    try:
        now = datetime.now(KST)
        start = start_month or _month_offset(now, -1)
        end = end_month or _month_offset(now, 3)
        months_between(start, end)
        account = credentials(paths.project_root() / "secrets" / "saveticker.env")
        from playwright.sync_api import sync_playwright
    except ImportError:
        typer.echo(
            "uv sync --extra browser 및 uv run playwright install chromium을 실행하세요", err=True
        )
        raise typer.Exit(1) from None
    except (ValueError, ConfigError) as exc:
        typer.echo(
            str(exc) if isinstance(exc, ConfigError) else "캘린더 월 범위가 잘못되었습니다",
            err=True,
        )
        raise typer.Exit(1) from None
    from trader.daemon import Lock

    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    run_folder = runtime()
    run_folder.mkdir(parents=True, exist_ok=True)
    lock = Lock(run_folder / (f"collector-{lane}.lock" if lane else "collector.lock"))
    if not lock.acquire():
        typer.echo("SaveTicker 수집기가 이미 실행 중입니다", err=True)
        raise typer.Exit(1)
    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    import os

    def heartbeat(state, **kwargs):
        atomic_json(
            run_folder / (f"runner-{lane}.json" if lane else "runner.json"),
            {
                "pid": os.getpid(),
                "state": state,
                "updated_at": datetime.now(UTC).isoformat(),
                **kwargs,
            },
        )

    adapter = None
    last_workers = {}

    def worker_status(state):
        nonlocal last_workers
        last_workers = state.get("workers", {})
        heartbeat("supervising", **state)

    try:
        if not once and lane is None:
            from trader.sources.saveticker.supervisor import supervise

            planned = build_views(start, end) + [
                View("news_all", "news"),
                View("news_reports", "news"),
            ]
            if only:
                requested = set(only.split(","))
                if requested - {view.stream for view in planned}:
                    raise ConfigError("알 수 없는 수집 스트림입니다")
                planned = [view for view in planned if view.stream in requested]
            commands = {}
            for group in sorted({lane_for(view) for view in planned}):
                command = [
                    sys.executable,
                    "-m",
                    "trader",
                    "saveticker",
                    "run",
                    "--lane",
                    group,
                    "--headed" if headed else "--headless",
                    "--start-month",
                    start,
                    "--end-month",
                    end,
                    "--max-scrolls",
                    str(max_scrolls),
                    "--max-details",
                    str(max_details),
                ]
                if only:
                    command.extend(["--only", only])
                commands[group] = command
            _print({"state": "supervising", "lanes": list(commands), "root": str(folder)})
            supervise(commands, run_folder, stop, worker_status)
            heartbeat("stopped")
            return
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            try:
                context = browser.new_context(
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                adapter = Browser(
                    page,
                    account,
                    max_scrolls=max_scrolls,
                    max_details=min(max_details, 2) if not once else max_details,
                    artifact_root=folder,
                    max_reports=max_details if once else 2,
                )
                heartbeat("logging_in")
                adapter.login()
                news_views = adapter.discover_news() if lane in (None, "news", "background") else []
                views = build_views(start, end) + news_views
                if only:
                    requested = set(only.split(","))
                    known = build_views(start, end) + [
                        View("news_all", "news"),
                        View("news_reports", "news"),
                    ]
                    if requested - {view.stream for view in known}:
                        raise ConfigError("알 수 없는 수집 스트림입니다")
                    views = [view for view in views if view.stream in requested]
                if lane:
                    views = [view for view in views if lane_for(view) == lane]
                store = Store(folder)
                _print({"state": "collecting", "streams": len(views), "root": str(folder)})
                due = {v.stream: 0.0 for v in views}
                completed = set()
                hot_streak = 0
                while not stop.is_set():
                    candidates = [v for v in views if not once or v.stream not in completed]
                    if not candidates:
                        break
                    view = (
                        min(candidates, key=lambda v: due[v.stream])
                        if once
                        else choose_due(
                            candidates, due, now=time.monotonic(), hot_streak=hot_streak
                        )
                    )
                    if view is None:
                        stop.wait(min(5, max(0, min(due.values()) - time.monotonic())))
                        continue
                    hot_streak = hot_streak + 1 if view.interval_s <= 30 else 0
                    begin = time.monotonic()
                    heartbeat(
                        "collecting",
                        stream=view.stream,
                        lag_s=max(0, begin - due[view.stream]) if due[view.stream] else 0,
                    )
                    result = collect_cycle(adapter, store, [view])[0]
                    heartbeat(
                        "collecting",
                        stream=view.stream,
                        diagnostics=adapter.diagnostics,
                        elapsed_s=time.monotonic() - begin,
                    )
                    _print(result)
                    completed.add(view.stream)
                    interval = (
                        60
                        if view.stream == "news_reports" and adapter.pending_reports
                        else view.interval_s
                    )
                    due[view.stream] = max(begin + interval, time.monotonic() + 2)
                    if not adapter.authenticated():
                        heartbeat("reauthenticating")
                        adapter.login()
            finally:
                browser.close()
        heartbeat("stopped")
    except Exception as exc:
        # Playwright 오류에는 폼 입력이 들어갈 수 있으므로 메시지는 절대 출력하지 않는다.
        diagnostics = dict(adapter.diagnostics) if adapter else {}
        diagnostics.update(sqlite_diagnostics(exc))
        heartbeat(
            "error",
            error_type=type(exc).__name__,
            workers=last_workers,
            stage=adapter.stage if adapter else "browser_start",
            diagnostics=diagnostics,
        )
        typer.echo(
            f"수집 중단: {type(exc).__name__} (runner.json의 단계·HTTP 상태를 확인하세요)", err=True
        )
        raise typer.Exit(1) from None
    finally:
        lock.release()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
