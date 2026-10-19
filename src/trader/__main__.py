"""`python -m trader` / 번들 실행파일 진입점.

번들을 인자 없이(더블클릭) 실행하면 `trader app`(첫 실행 초기화 → 데몬 + 웹 UI → 브라우저)으로 간다.
인자가 있으면 보통 CLI 다."""

from __future__ import annotations

import sys


def main() -> None:
    from trader import cli
    from trader.platform import bundle

    if bundle.is_frozen() and len(sys.argv) == 1:
        cli.app(["app"])
        return
    cli.app()


if __name__ == "__main__":
    main()
