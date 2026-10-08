import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from trader.broker.kis_auth import KisAuth
from trader.broker.kis_client import KisClient, RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def respx_mock():
    with respx.mock(assert_all_called=False) as m:
        yield m


def _client(env: str, project: Path) -> KisClient:
    auth = KisAuth(env, "key", "secret", clock=lambda: T0, state=project / "state")
    # 토큰은 캐시 파일로 미리 깔아 발급 호출을 생략한다
    (project / "state" / f"kis_token_{env}.json").write_text(
        json.dumps({"access_token": "tok", "expires_at": "2099-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    limiter = RateLimiter(10_000, clock=lambda: 0.0, sleep=lambda s: None)
    return KisClient(env, auth, limiter=limiter, http=httpx.Client(timeout=5))


@pytest.fixture
def paper_client(project, respx_mock):
    return _client("paper", project)


@pytest.fixture
def real_client(project, respx_mock):
    return _client("real", project)
