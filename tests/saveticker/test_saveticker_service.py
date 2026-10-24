from datetime import UTC, datetime


def test_all_requested_rankings_and_calendar_months_are_scheduled():
    from trader.sources.saveticker.service import build_views

    views = build_views("2026-12", "2027-01")
    streams = {v.stream for v in views}
    assert {
        "community_index_live",
        "community_gain_live",
        "community_loss_live",
        "market_turnover_live",
        "market_gain_live",
        "market_loss_live",
        "calendar_2026-12",
        "calendar_2027-01",
        "community_posts",
    } <= streams
    assert all(v.interval_s == 300 for v in views if v.kind == "calendar")
    assert {"market_turnover_regular", "community_gain_regular"} <= streams


def test_one_view_failure_does_not_skip_other_views(tmp_path):
    from trader.sources.saveticker.models import Capture
    from trader.sources.saveticker.service import View, collect_cycle
    from trader.sources.saveticker.store import Store

    class Browser:
        def collect(self, view):
            if view.stream == "news_all":
                raise RuntimeError("private-secret-should-not-appear")
            return Capture(
                stream=view.stream,
                page_url="https://www.saveticker.com/watchlist",
                observed_at=datetime.now(UTC),
                items=[{"id": "x", "title": "종목"}],
            )

    store = Store(tmp_path)
    results = collect_cycle(
        Browser(), store, [View("news_all", "news"), View("market_gain_live", "market")]
    )
    assert [r["status"] for r in results] == ["error", "ok"]
    assert "private-secret" not in (tmp_path / "status.json").read_text()


def test_virtual_scroll_accumulates_recycled_rows_and_marks_limit():
    from trader.sources.saveticker.models import Item
    from trader.sources.saveticker.service import accumulate

    batches = iter(
        [
            [Item(id="a", title="첫 행")],
            [Item(id="b", title="다음 행")],
            [Item(id="c", title="마지막 행")],
        ]
    )
    rows, reason = accumulate(lambda: next(batches), lambda: None, max_steps=2)
    assert {x.id for x in rows} == {"a", "b"}
    assert reason == "scroll_limit"


def test_stable_window_never_claims_whole_site_complete():
    from trader.sources.saveticker.models import Item
    from trader.sources.saveticker.service import accumulate

    rows, reason = accumulate(lambda: [Item(id="a", title="행")], lambda: None, max_steps=8)
    assert len(rows) == 1
    assert reason == "no_progress_unverified_end"


def test_credentials_missing_never_echoes_values(tmp_path):
    import pytest

    from trader.config import ConfigError
    from trader.sources.saveticker.browser import credentials

    p = tmp_path / "saveticker.env"
    p.write_text("SAVETICKER_EMAIL='private@example.com'\n")
    with pytest.raises(ConfigError) as exc:
        credentials(p)
    assert "private@example.com" not in str(exc.value)


def test_credentials_preserves_dollar_signs_without_env_interpolation(tmp_path):
    from trader.sources.saveticker.browser import credentials

    p = tmp_path / "saveticker.env"
    p.write_text("SAVETICKER_EMAIL='test@example.com'\nSAVETICKER_PASSWORD='${HOME}literal'\n")
    assert credentials(p)[1] == "${HOME}literal"


def test_login_waits_for_hydrated_email_dialog_before_filling():
    """SSR 버튼이 먼저 보이고 클릭 핸들러가 늦게 붙어도 로그인한다."""
    import pytest

    api = pytest.importorskip("playwright.sync_api")
    from trader.sources.saveticker.browser import Browser

    html = """<meta charset="utf-8"><button id="email">이메일로 로그인</button>
    <script>setTimeout(()=>document.querySelector('#email').onclick=()=>{
      document.body.innerHTML='<h3>이메일로 로그인</h3><input type="email">'
       +'<input type="password"><button id="submit">로그인</button>';
      document.querySelector('#submit').onclick=()=>{
        document.body.innerHTML='<button aria-label="프로필 메뉴"></button>';
      };
    },4000)</script>"""
    with api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except api.Error:
            pytest.skip("선택 의존성 Chromium 미설치")
        try:
            page = browser.new_page()
            page.set_default_timeout(1000)
            page.route(
                "https://www.saveticker.com/**",
                lambda route: route.fulfill(body=html, content_type="text/html"),
            )
            adapter = Browser(page, ("test@example.com", "test-password"))
            adapter.login()
            assert adapter.authenticated()
        finally:
            browser.close()


def test_community_comment_budget_reaches_second_post_next_cycle():
    import pytest

    api = pytest.importorskip("playwright.sync_api")
    import html
    import json

    from trader.sources.saveticker.browser import Browser
    from trader.sources.saveticker.dom import extract

    cards = []
    for index in (1, 2):
        fragment = (
            f'<div data-type="comment"><div><span class="text-small-bold">독자</span>'
            f"<p>댓글{index}</p></div></div>"
        )
        onclick = html.escape(
            "this.parentElement.parentElement.parentElement.insertAdjacentHTML("
            "'beforeend'," + json.dumps(fragment) + ")",
            quote=True,
        )
        cards.append(f"""<div><div><div><span class="text-small-bold">독자{index}</span>
          <span>1시간 전</span></div><p class="whitespace-pre-wrap">원글{index}</p></div>
          <div><div><button data-selected="false" aria-pressed="false">0</button>
          <button data-selected="false" onclick="{onclick}">1</button></div>
          <button aria-label="더보기 메뉴"></button></div></div>""")
    with api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except api.Error:
            pytest.skip("선택 의존성 Chromium 미설치")
        try:
            page = browser.new_page()
            page.set_content("".join(cards))
            adapter = Browser(page, ("unused", "unused"), max_details=1)
            first = adapter._post_details(extract(page.content(), "community_posts"))
            assert [x.body for x in first if x.kind == "comment"] == ["댓글1"]
            adapter.detail_requested = 0
            second = adapter._post_details(extract(page.content(), "community_posts"))
            assert "댓글2" in [x.body for x in second if x.kind == "comment"]
        finally:
            browser.close()


def test_hot_list_refresh_has_priority_but_background_is_not_starved():
    from trader.sources.saveticker.service import View, choose_due

    views = [
        View("calendar_2026-10", "calendar", interval_s=300),
        View("news_all", "news", interval_s=30),
    ]
    due = {v.stream: 0 for v in views}
    assert choose_due(views, due, now=100, hot_streak=0).stream == "news_all"
    assert choose_due(views, due, now=100, hot_streak=8).stream == "calendar_2026-10"
    assert choose_due(views, {v.stream: 200 for v in views}, now=100) is None


def test_calendar_all_filter_does_not_click_all_range_button():
    import pytest

    api = pytest.importorskip("playwright.sync_api")
    from trader.sources.saveticker.browser import Browser

    with api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except api.Error:
            pytest.skip("선택 의존성 Chromium 미설치")
        try:
            page = browser.new_page()
            page.set_content("""<button onclick="document.body.dataset.range='all'">전체</button>
              <button aria-pressed="false"
                onclick="this.setAttribute('aria-pressed','true')">전체</button>""")
            adapter = Browser(page, ("unused", "unused"))
            adapter._toggle("전체", True)
            assert page.locator("body").get_attribute("data-range") is None
            assert page.locator("button[aria-pressed]").get_attribute("aria-pressed") == "true"
        finally:
            browser.close()
