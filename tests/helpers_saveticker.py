"""수집기 테스트 공통: Chromium 이 없는 환경(CI)에서는 브라우저 테스트를 건너뛴다."""

import pytest


def launch_or_skip(p):
    from playwright.sync_api import Error

    try:
        return p.chromium.launch()
    except Error:
        pytest.skip("Chromium 미설치(uv run trader saveticker install-browser)")
