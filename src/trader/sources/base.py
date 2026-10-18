"""수집 결과 Section 래퍼."""

from __future__ import annotations

import contextlib
import html
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx

logger = logging.getLogger("trader.sources")

# 의도적으로 건너뛴 절의 error 값. 00_inputs.errors 에 집계하지 않는다.
SKIP_REASONS = frozenset(
    {"disabled", "phase6", "phase7", "phase8", "not available for US", "no manual input"}
)


class Section(TypedDict):
    data: Any
    received_at: str
    error: str | None


def utc_now() -> datetime:
    return datetime.now(UTC)


_utc_now = utc_now
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str | None) -> str:
    """HTML 태그 제거. RSS 는 이스케이프해서, 네이버는 태그 그대로 주므로 unescape 를 두 번 한다."""
    once = html.unescape(text or "")
    return html.unescape(TAG_RE.sub("", once)).replace("\xa0", " ").strip()


def run_section(
    fn: Callable[[], Any],
    *,
    enabled: bool = True,
    now: Callable[[], datetime] = _utc_now,
) -> Section:
    """수집 함수를 실행해 Section 으로 감싼다. 예외는 던지지 않고 error 에 기록한다."""
    if not enabled:
        return Section(data=None, received_at=now().isoformat(), error="disabled")
    try:
        data = fn()
    except Exception as e:  # noqa: BLE001 - 수집 실패는 절 단위로 격리한다
        logger.warning("수집 실패: %s: %s", type(e).__name__, e)
        return Section(data=None, received_at=now().isoformat(), error=f"{type(e).__name__}: {e}")
    return Section(data=data, received_at=now().isoformat(), error=None)


def skipped_section(reason: str, *, now: Callable[[], datetime] = _utc_now) -> Section:
    """호출 없이 건너뛴 절."""
    return Section(data=None, received_at=now().isoformat(), error=reason)


def owned_client(
    http: httpx.Client | None, *, timeout: float
) -> AbstractContextManager[httpx.Client]:
    """주입된 클라이언트는 그대로 쓰고(호출자가 닫음), 없으면 새로 만들어 블록 끝에 닫는다."""
    if http is not None:
        return contextlib.nullcontext(http)
    return httpx.Client(timeout=timeout)
