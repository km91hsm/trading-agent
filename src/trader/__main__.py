"""`python -m trader` / 번들 실행파일 진입점.

번들을 인자 없이(더블클릭) 실행하면 `trader app`(첫 실행 초기화 → 데몬 + 웹 UI → 브라우저)으로 간다.
인자가 있으면 보통 CLI 다. Windows 더블클릭은 오류로 끝나면 콘솔이 바로 닫혀 메시지를 못 보므로
종료 전에 Enter 를 기다린다."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _pause_if_console() -> None:
    if sys.platform != "win32" or sys.stdin is None:
        return
    if getattr(sys.stdin, "isatty", lambda: False)():
        try:
            input("오류로 종료됐습니다. Enter 를 누르면 닫힙니다")
        except EOFError:
            pass


def main() -> None:
    from trader import cli
    from trader.platform import bundle

    # 콘솔·파이프 어느 쪽이든 UTF-8 (Windows 는 파이프면 cp1252 라 한글 도움말에서 죽는다)
    cli._force_utf8()
    if not (bundle.is_frozen() and len(sys.argv) == 1):
        cli.app()
        return
    # 창 없는 실행파일(TraderApp)은 트레이 모드, 콘솔 trader 는 지금처럼 터미널 모드
    windowed = Path(sys.executable).stem.lower().startswith("traderapp")
    try:
        cli.app(["app", "--tray"] if windowed else ["app"])
    except SystemExit as e:
        if e.code not in (0, None):
            _pause_if_console()
        raise
    except Exception:
        traceback.print_exc()
        _pause_if_console()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
