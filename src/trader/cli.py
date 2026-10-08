"""`trader` 명령행 진입점 (typer)."""

from __future__ import annotations

import sys

import typer

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
    """state/STATUS.md 를 재생성한다."""
    typer.echo("아직 구현 전입니다 (Phase 0 Task 6).")
