"""전체 화면 수집 계획과 스트림별 실패 격리·변경 저장."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from trader.sources.saveticker.dom import months_between
from trader.sources.saveticker.models import Capture, Item, ViewInfo
from trader.sources.saveticker.store import Store

PATHS = {
    "news": "/news",
    "community": "/community",
    "market": "/watchlist",
    "calendar": "/calendar",
}


class FatalCollectionError(RuntimeError):
    """인증·접근 실패는 자동 반복하지 않고 수집기를 중단한다."""


@dataclass(frozen=True)
class View:
    stream: str
    kind: str
    tab: str | None = None
    source: str | None = None
    category: str | None = None
    month: str | None = None
    regular: bool = False
    interval_s: float = 20
    ticker: str | None = None

    @property
    def url(self) -> str:
        return "https://www.saveticker.com" + PATHS[self.kind]


def lane_for(view: View) -> str:
    if view.kind == "calendar" or view.stream == "news_reports":
        return "background"
    return view.kind


def build_views(start_month: str, end_month: str) -> list[View]:
    views = [View("community_trends", "community"), View("community_top5", "community")]
    for kind, choices in [
        ("community", [("index", "지수"), ("gain", "상승"), ("loss", "하락")]),
        ("market", [("turnover", "거래대금"), ("gain", "상승"), ("loss", "하락")]),
    ]:
        for slug, label in choices:
            for regular in (False, True):
                basis = "regular" if regular else "live"
                views.append(
                    View(
                        f"{kind}_{slug}_{basis}",
                        kind,
                        tab=label,
                        regular=regular,
                        interval_s=300 if regular else 20,
                    )
                )
    for month in months_between(start_month, end_month):
        views.append(View(f"calendar_{month}", "calendar", month=month, interval_s=300))
    return views


def accumulate(
    read: Callable[[], list[Item]], scroll: Callable[[], None], *, max_steps: int
) -> tuple[list[Item], str]:
    """가상 스크롤에서 재활용된 DOM 행을 누적. 정지해도 전체 완료로 추측하지 않는다."""
    rows: dict[str, Item] = {}
    stable = 0
    for _ in range(max_steps):
        before = set(rows)
        rows.update({item.id: item for item in read()})
        stable = stable + 1 if set(rows) == before else 0
        if stable >= 3:
            return list(rows.values()), "no_progress_unverified_end"
        scroll()
    return list(rows.values()), "scroll_limit"


def collect_cycle(browser, store: Store, views: list[View]) -> list[dict]:
    results = []
    for view in views:
        begin = time.monotonic()
        try:
            capture = browser.collect(view)
        except FatalCollectionError:
            raise
        except Exception as exc:
            # 브라우저 예외에 폼 값·URL 인자가 포함될 수 있어 타입만 기록한다.
            capture = Capture(
                stream=view.stream,
                page_url=view.url,
                observed_at=datetime.now(UTC),
                status="error",
                reason=type(exc).__name__,
                items=[],
            )
        capture.view = ViewInfo(
            source=view.source,
            category=view.category,
            tab=view.tab,
            month=view.month,
            requested_regular=view.regular if view.tab else None,
        )
        capture.elapsed_s = time.monotonic() - begin
        capture.target_interval_s = view.interval_s
        results.append(store.save(capture))
    return results


def choose_due(
    views: list[View], due: dict[str, float], *, now: float, hot_streak: int = 0
) -> View | None:
    """핵심 목록을 우선하되 연속 8회 뒤에는 대기 중인 전체 탭 한 개를 처리한다."""
    ready = [v for v in views if due[v.stream] <= now]
    hot = [v for v in ready if v.interval_s <= 30]
    cold = [v for v in ready if v.interval_s > 30]
    group = cold if cold and hot_streak >= 8 else hot or cold
    return min(group, key=lambda v: due[v.stream]) if group else None
