"""`trader` 명령행 진입점 (typer)."""

from __future__ import annotations

import sys

import typer

from trader import config
from trader.logging_setup import configure
from trader.platform import paths

app = typer.Typer(help="LLM 멀티 에이전트 주식 자동매매", no_args_is_help=True)


def _force_utf8() -> None:
    """콘솔 출력을 UTF-8 로 고정한다 (Windows cp949 대비)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def main() -> None:
    _force_utf8()


@app.command()
def status() -> None:
    """설정·원장·TODO 를 읽어 state/STATUS.md 를 재생성한다."""
    from trader import status as status_mod
    from trader.ledger import db

    try:
        configure(paths.state_dir())
        settings = config.load_settings()
        accounts = config.load_accounts()
        conn = db.connect()
        try:
            db.init_schema(conn)
            db.sync_accounts(conn, accounts)
            text = status_mod.build_status(conn, settings, accounts, paths.docs_dir() / "TODO.md")
        finally:
            conn.close()
        out = status_mod.write_status(text)
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"저장: {out}")
