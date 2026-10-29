"""`python -m trader` / 번들 실행파일 진입점.

번들을 인자 없이(더블클릭) 실행하면 `trader app`(첫 실행 초기화 → 데몬 + 웹 UI → 브라우저)으로 간다.
인자가 있으면 보통 CLI 다. Windows 더블클릭은 오류로 끝나면 콘솔이 바로 닫혀 메시지를 못 보므로
종료 전에 Enter 를 기다린다."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def translocated(executable: str) -> bool:
    """격리 속성이 남은 앱을 '우클릭 → 열기' 로 실행하면 Gatekeeper 가 읽기 전용 임시 경로
    (`.../AppTranslocation/<UUID>/d/Trader.app`)에서 돌린다 — TRADER_HOME 이 그 경로가 된다."""
    return "/AppTranslocation/" in executable


def _ensure_streams() -> None:
    """창 없는 실행파일(Windows TraderApp)은 stdout/stderr 가 None 이다 — 그대로 두면
    `print`·`isatty()`·라이브러리 로그 설정이 AttributeError 로 죽는다. devnull 로 바꿔 둔다."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _record_startup_error(text: str) -> None:
    """창 없는 앱의 시작 실패는 화면·콘솔 어디에도 안 보인다 — state/logs/startup-error.log 에
    남기고(경로를 못 얻으면 홈) 가능하면 OS 알림을 띄운다(전체 검토 2026-10-27)."""
    try:
        from trader.platform import paths

        log_dir = paths.state_dir() / "logs"
    except Exception:  # noqa: BLE001 - 경로 자체가 실패 원인일 수 있다
        log_dir = Path.home()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "startup-error.log").open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    try:
        from trader.platform import notify_os

        notify_os.alert("Trader 시작 실패", text.strip().splitlines()[-1][:200])
    except Exception:  # noqa: BLE001
        pass


def _pause_if_console() -> None:
    if sys.platform != "win32" or sys.stdin is None:
        return
    if getattr(sys.stdin, "isatty", lambda: False)():
        try:
            input("오류로 종료됐습니다. Enter 를 누르면 닫힙니다")
        except EOFError:
            pass


def main() -> None:
    # Chrome supplies the caller origin as argv[1]; keep binary framing away from CLI output.
    if len(sys.argv) > 1:
        from trader.saveticker.bridge_install import is_native_origin, native_entry

        if is_native_origin(sys.argv[1]):
            code = native_entry(sys.argv[1])
            if code:
                raise SystemExit(code)
            return
    from trader import cli
    from trader.platform import bundle

    _ensure_streams()
    # 콘솔·파이프 어느 쪽이든 UTF-8 (Windows 는 파이프면 cp1252 라 한글 도움말에서 죽는다)
    cli._force_utf8()
    if not (bundle.is_frozen() and len(sys.argv) == 1):
        cli.app()
        return
    # 창 없는 실행파일(TraderApp)은 트레이 모드, 콘솔 trader 는 지금처럼 터미널 모드
    windowed = Path(sys.executable).stem.lower().startswith("traderapp")
    if translocated(sys.executable):
        msg = (
            "macOS 가 앱을 격리된 임시 경로(App Translocation)에서 실행했어요 — "
            "여기서는 설정·원장을 만들 수 없습니다. 터미널에서 "
            "`xattr -dr com.apple.quarantine <압축 푼 trader 폴더>` 를 실행하거나 "
            "Finder 로 폴더를 다른 곳에 옮긴 뒤 다시 여세요."
        )
        print(msg, file=sys.stderr)
        _record_startup_error(msg)
        raise SystemExit(1)
    try:
        cli.app(["app", "--tray"] if windowed else ["app"])
    except SystemExit as e:
        if e.code not in (0, None):
            if windowed:
                _record_startup_error(
                    f"종료 코드 {e.code} — 설정 오류·데몬 잠금 등 (trader app 로그 참고)"
                )
            _pause_if_console()
        raise
    except Exception:
        traceback.print_exc()
        if windowed:
            _record_startup_error(traceback.format_exc())
        _pause_if_console()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
