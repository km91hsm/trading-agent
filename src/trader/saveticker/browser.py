"""독립 Playwright 브라우저 어댑터. 계정 파일은 읽기만 하며 trace를 남기지 않는다."""

from __future__ import annotations

import json
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from trader.config import ConfigError
from trader.saveticker import dom, jsonfeed
from trader.saveticker.models import Attachment, Capture
from trader.saveticker.service import FatalCollectionError, View, accumulate
from trader.saveticker.store import atomic_json


class SiteClientError(RuntimeError):
    """사이트가 클라이언트 자바스크립트 예외 화면을 표시한다."""


class AccessDenied(FatalCollectionError):
    """사이트가 접근을 거부했다. 우회하거나 자동 재시도하지 않는다."""


class LoginRequired(FatalCollectionError):
    """인증되지 않았거나 재인증에 실패했다."""


def credentials(path: Path) -> tuple[str, str]:
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    email, password = values.get("SAVETICKER_EMAIL"), values.get("SAVETICKER_PASSWORD")
    if not email or not password:
        raise ConfigError(
            "secrets/saveticker.env에 SAVETICKER_EMAIL과 SAVETICKER_PASSWORD가 필요합니다"
        )
    return email, password


class Browser:
    def __init__(
        self,
        page,
        account: tuple[str, str],
        *,
        max_scrolls: int = 20,
        max_details: int = 20,
        artifact_root: Path | None = None,
        max_reports: int = 2,
    ):
        self.max_reports = max_reports
        self.pending_reports = False
        self.artifact_root = artifact_root
        self.page = page
        self.account = account
        self.max_scrolls = max_scrolls
        self.max_details = max_details
        self.last_action = 0.0
        self.loaded_at = 0.0
        self.stage = "created"
        self.diagnostics = {"page_error_count": 0, "page_error_types": []}
        self.detail_cache = {}
        self.comment_cache = {}
        self.report_attempts = {}
        self.seen_streams = set()
        self.detail_requested = 0
        self.json_responses = []
        self.page.on("response", self._json_response)
        self.page.on("pageerror", self._page_error)

    def _json_response(self, response):
        if jsonfeed.route(response.url):
            self.json_responses.append(response)
            self.json_responses = self.json_responses[-200:]

    def _json_rows(self, view):
        if view.stream == "news_reports":
            return dom.extract(self.html(), view.stream)
        route = (
            "news"
            if view.stream == "news_all"
            else "calendar"
            if view.month
            else "top5"
            if view.stream == "community_top5"
            else "index"
            if "_index_" in view.stream
            else "popular"
        )
        responses = [r for r in self.json_responses if jsonfeed.route(r.url) == route]
        if route != "news":
            responses = responses[-1:]
        rows = {}
        for response in responses:
            if response.status in (401, 403, 429):
                raise AccessDenied("json_access_denied")
            if response.status != 200:
                raise ValueError("json_response_failed")
            value = response.json()
            for item in jsonfeed.extract(
                value, view.stream, month=view.month, regular=view.regular
            ):
                rows[item.id] = item
        self.diagnostics["collection_method"] = "browser_json"
        return list(rows.values())

    def _page_error(self, error):
        # 오류 원문 메시지/stack에는 토큰 URL이나 계정이 포함될 수 있다.
        name = getattr(error, "name", "Error")
        allowed = {
            "Error",
            "TypeError",
            "ReferenceError",
            "SyntaxError",
            "RangeError",
            "URIError",
            "EvalError",
        }
        name = name if name in allowed else "Error"
        self.diagnostics["page_error_count"] = self.diagnostics.get("page_error_count", 0) + 1
        self.diagnostics["page_error_types"] = sorted(
            set(self.diagnostics.get("page_error_types", []) + [name])
        )
        self.diagnostics["page_error_path"] = urlsplit(self.page.url).path

    def pace(self):
        remaining = 2 - (time.monotonic() - self.last_action)
        if remaining > 0:
            time.sleep(remaining)
        self.last_action = time.monotonic()

    def goto(self, url):
        self.pace()
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(1000)
        self.loaded_at = time.monotonic()
        self.diagnostics.update(
            {
                "http_status": response.status if response else None,
                "url_path": urlsplit(self.page.url).path,
            }
        )

        if response and response.status in (401, 403, 429):
            raise AccessDenied("site_access_denied")

    def click(self, locator):
        self.pace()
        locator.click(timeout=10000)
        self.page.wait_for_timeout(600)

    def html(self):
        # 메모리에서만 해석하고 scripts·폼·비밀번호는 파일로 내보내지 않는다.
        return self.page.locator("body").inner_html()

    def authenticated(self):
        return self.page.get_by_role("button", name="프로필 메뉴", exact=True).count() > 0

    def login(self):
        self.stage = "login_profile"
        self.goto("https://www.saveticker.com/profile")
        if self.authenticated():
            return
        body = self.page.locator("body").inner_text()
        self.diagnostics.update(
            {
                "challenge_visible": any(
                    s in body
                    for s in ("Verify you are human", "Checking your browser", "Just a moment")
                ),
                "login_text_visible": "이메일로 로그인" in body,
            }
        )
        self.stage = "login_email_button"
        email_button = self.page.get_by_role("button", name="이메일로 로그인", exact=True)
        email_button.wait_for(state="visible", timeout=15000)
        dialog_heading = self.page.get_by_role("heading", name="이메일로 로그인", exact=True)
        self.stage = "login_email_dialog"
        for attempt in range(3):
            self.click(email_button)
            try:
                dialog_heading.wait_for(state="visible", timeout=2000)
                break
            except Exception:
                if attempt == 2:
                    raise LoginRequired("email_dialog_not_ready") from None
        self.stage = "login_email_field"
        self.diagnostics.update(
            {
                "inputs": self.page.locator("input").evaluate_all(
                    "els=>els.map(e=>({type:e.type,autocomplete:e.autocomplete,visible:!!e.getClientRects().length}))"
                ),
                "email_dialog": self.page.get_by_role(
                    "heading", name="이메일로 로그인", exact=True
                ).count(),
            }
        )
        self.page.locator(
            'input[type="email"], input[autocomplete="email"], input[type="text"]'
        ).first.fill(self.account[0])
        self.stage = "login_password_field"
        self.page.locator('input[type="password"]').fill(self.account[1])
        self.stage = "login_submit"
        submit = self.page.get_by_role("button", name="로그인", exact=True)
        self.diagnostics.update(
            {
                "submit_count": submit.count(),
                "submit_enabled": submit.first.is_enabled() if submit.count() else False,
                "inputs": self.page.locator("input").evaluate_all(
                    "els => els.map(e=>({type:e.type,valid:e.validity.valid,filled:!!e.value}))"
                ),
            }
        )
        self.click(submit)
        self.stage = "login_result"
        try:
            self.page.get_by_role("button", name="프로필 메뉴", exact=True).wait_for(
                state="visible", timeout=20000
            )
        except Exception:
            raise LoginRequired("login_failed") from None

    def discover_news(self) -> list[View]:
        self.goto("https://www.saveticker.com/news")
        self.page.locator('[role="tablist"]').first.wait_for(timeout=15000)
        # 2026-10-25 UI 비교: 전체568건에 각 소스표본20건씩 모두 포함.
        # 동일 기사를 소스/카테고리별로 재수집하지 않고 DOM의 배지로 분류한다.
        views = [View("news_all", "news", source="전체", category="전체")]
        views.append(View("news_reports", "news", source="SAVE 리포트", interval_s=900))
        return views

    def _toggle(self, name: str, value: bool):
        control = self.page.get_by_role("checkbox", name=name, exact=True).or_(
            self.page.get_by_role("button", name=name, exact=True).and_(
                self.page.locator("[aria-pressed], [aria-checked]")
            )
        )
        if not control.count():
            return
        node = control.first
        if not node.is_enabled():
            return
        # 사이트는 aria-pressed 버튼도 체크박스로 노출한다.
        current = node.get_attribute("aria-checked") or node.get_attribute("aria-pressed")
        if (current == "true") != value:
            self.click(node)

    def _calendar(self, view: View):
        self.click(self.page.get_by_role("button", name="월별", exact=True))
        # 월별 범위의 이전·다음은 달력 격자의 이전 달·다음 달과 다르다.
        for _ in range(26):
            current = dom.calendar_month(self.html())
            if current == view.month:
                return
            label = "다음" if current < view.month else "이전"
            self.click(self.page.get_by_role("button", name=label, exact=True))
        raise ValueError("calendar_month_not_reached")

    def _scroll(self):
        self.pace()
        if self.page.locator("#news-tabpanel").count():
            self.page.evaluate("""() => {
              for (let e=document.querySelector('#news-tabpanel');e;e=e.parentElement) {
                if(e.clientHeight > 100 && e.scrollHeight > e.clientHeight + 20 &&
                   ['auto','scroll'].includes(getComputedStyle(e).overflowY)) {
                  e.scrollBy(0, e.clientHeight * .8); return;
                }
              }
            }""")
            self.page.wait_for_timeout(700)
            return
        self.page.evaluate("""() => {
          const nodes = [...document.querySelectorAll('*')].filter(e =>
            e.clientHeight > 100 && e.scrollHeight > e.clientHeight + 30 &&
            ['auto','scroll'].includes(getComputedStyle(e).overflowY));
          const e = nodes.sort((a,b)=>b.clientHeight-a.clientHeight)[0];
          if (e) e.scrollBy(0, Math.max(200, e.clientHeight * .7));
          else window.scrollBy(0, window.innerHeight * .7);
        }""")
        self.page.wait_for_timeout(700)

    def _details(self, items):
        result = []
        for item in items:
            cached = self.detail_cache.get(item.id)
            if cached and time.monotonic() - cached[0] < 300:
                detail = cached[1]
                result.append(
                    item.model_copy(
                        update={
                            k: getattr(detail, k) for k in ("body", "published_at", "detail_status")
                        }
                    )
                )
                continue
            card = self.page.locator("#news-tabpanel").get_by_role(
                "button", name=item.title, exact=True
            )
            if self.detail_requested >= self.max_details or not card.count():
                result.append(item.model_copy(update={"detail_status": "pending"}))
                continue
            self.detail_requested += 1
            try:
                self.click(card.first)
                self.page.get_by_role("heading", name=item.title, exact=True).first.wait_for(
                    timeout=10000
                )
                detail = item.model_copy(update={"detail_status": "pending"})
                for _ in range(20):
                    for response in reversed(self.json_responses):
                        if jsonfeed.route(response.url) != "detail":
                            continue
                        if response.status in (401, 403, 429):
                            raise AccessDenied("json_detail_access_denied")
                        if response.status != 200:
                            continue
                        value = response.json()
                        converted = jsonfeed.news_item(value, detail=True)
                        if converted.title == item.title:
                            detail = item.model_copy(
                                update={
                                    k: getattr(converted, k)
                                    for k in ("body", "published_at", "detail_status")
                                }
                            )
                            break
                    if detail.detail_status == "ok":
                        break
                    self.page.wait_for_timeout(250)
                result.append(detail)
                if detail.detail_status == "ok":
                    self.detail_cache[item.id] = (time.monotonic(), detail)
            except FatalCollectionError:
                raise
            except Exception as exc:
                # 오류 메시지에는 민감한 URL/폼 내용이 있을 수 있어 타입만 보존한다.
                self.diagnostics["detail_error_type"] = type(exc).__name__
                self.diagnostics["detail_error_count"] = (
                    self.diagnostics.get("detail_error_count", 0) + 1
                )
                result.append(item.model_copy(update={"detail_status": "unavailable"}))
            finally:
                # Escape는 상세만 닫고 원래 가상 목록과 스크롤 위치를 보존한다(실기 확인).
                self.pace()
                self.page.keyboard.press("Escape")
                self.page.locator("#news-tabpanel").wait_for(state="visible", timeout=10000)
        return result

    def _reports(self, items):
        from trader.saveticker.reports import save_pdf

        rows = []
        order = {item.id: index for index, item in enumerate(items)}
        for item in sorted(items, key=lambda row: self.report_attempts.get(row.id, 0)):
            if self.artifact_root is None:
                rows.append(item.model_copy(update={"detail_status": "storage_unavailable"}))
                continue
            manifest = self.artifact_root / "attachments" / f"report-{item.id}.json"
            try:
                cached = json.loads(manifest.read_text(encoding="utf-8"))
                attachment = Attachment.model_validate(cached["attachment"])
                path = self.artifact_root / attachment.pdf_path
                age = time.time() - cached["fetched_at"]
                if (
                    0 <= age < 86400
                    and path.is_file()
                    and path.stat().st_size == attachment.size_bytes
                ):
                    import hashlib

                    if hashlib.sha256(path.read_bytes()).hexdigest() == attachment.sha256:
                        rows.append(
                            item.model_copy(
                                update={
                                    "attachment": attachment,
                                    "detail_status": "ok" if attachment.text_path else "pdf_only",
                                }
                            )
                        )
                        continue
            except (ValueError, KeyError, OSError, TypeError):
                pass
            if self.detail_requested >= self.max_reports:
                rows.append(item.model_copy(update={"detail_status": "pending"}))
                continue
            button = None
            buttons = self.page.get_by_role("button", name="PDF 다운로드", exact=True)
            for index in range(buttons.count()):
                candidate = buttons.nth(index)
                scope = candidate
                for _ in range(5):
                    scope = scope.locator("xpath=..")
                    if scope.get_by_role("button", name="PDF 다운로드", exact=True).count() != 1:
                        break
                    if (
                        scope.locator("p")
                        .filter(has_text=re.compile("^" + re.escape(item.title) + "$"))
                        .count()
                    ):
                        button = candidate
                        break
                if button is not None:
                    break
            if button is None:
                rows.append(item.model_copy(update={"detail_status": "pending"}))
                continue
            self.detail_requested += 1
            self.report_attempts[item.id] = time.monotonic()
            try:
                with self.page.expect_download(timeout=30000) as pending:
                    self.click(button)
                with tempfile.TemporaryDirectory(prefix="saveticker-report-") as temporary:
                    path = Path(temporary) / "report.pdf"
                    pending.value.save_as(path)
                    attachment = save_pdf(path, self.artifact_root)
                atomic_json(
                    manifest, {"attachment": attachment.model_dump(), "fetched_at": time.time()}
                )
                rows.append(
                    item.model_copy(
                        update={
                            "attachment": attachment,
                            "detail_status": "ok" if attachment.text_path else "pdf_only",
                        }
                    )
                )
            except Exception as exc:
                self.diagnostics["report_error_type"] = type(exc).__name__
                rows.append(item.model_copy(update={"detail_status": "download_failed"}))
        return sorted(rows, key=lambda row: order[row.id])

    def _post_details(self, items):
        result = []
        menus = self.page.locator('button[aria-label="더보기 메뉴"]')
        for item in sorted(items, key=lambda x: self.comment_cache.get(x.id, (0,))[0]):
            cached = self.comment_cache.get(item.id)
            if cached and time.monotonic() - cached[0] < 300 and cached[1] == item.comment_count:
                result.append(item.model_copy(update={"detail_status": "ok"}))
                result.extend(cached[2])
                continue
            if not item.comment_count or item.comment_count == "0":
                result.append(item.model_copy(update={"detail_status": "ok"}))
                continue
            if self.detail_requested >= self.max_details:
                result.append(item.model_copy(update={"detail_status": "pending"}))
                continue
            found = False
            for index in range(menus.count()):
                card = menus.nth(index).locator("xpath=../..")
                body = card.locator("p.whitespace-pre-wrap")
                if not body.count() or body.first.inner_text().strip() != item.body:
                    continue
                found = True
                self.detail_requested += 1
                try:
                    button = card.locator("button[data-selected]:not([aria-pressed])").first
                    self.click(button)
                    self.page.wait_for_timeout(700)
                    for _ in range(self.max_scrolls):
                        more = card.get_by_role(
                            "button",
                            name=re.compile(r"^(?:댓글 )?더 ?보기(?: \d+개)?$|^답글 \d+개 보기$"),
                        )
                        if not more.count() or not more.first.is_enabled():
                            break
                        self.click(more.first)
                    children = dom.comments(card.inner_html(), parent_id=item.id)
                    complete = len(children) >= int(item.comment_count)
                    if complete:
                        self.comment_cache[item.id] = (
                            time.monotonic(),
                            item.comment_count,
                            children,
                        )
                    result.append(
                        item.model_copy(
                            update={"detail_status": "ok" if complete else "comments_partial"}
                        )
                    )
                    result.extend(children)
                except Exception:
                    result.append(item.model_copy(update={"detail_status": "unavailable"}))
                break
            if not found:
                result.append(item.model_copy(update={"detail_status": "pending"}))
        return result

    def scroll_steps(self, view: View) -> int:
        return (
            min(self.max_scrolls, 2)
            if view.stream == "news_all" and view.stream in self.seen_streams
            else self.max_scrolls
        )

    def collect(self, view: View) -> Capture:
        if view.stream in {"community_posts", "community_trends"} or view.stream.startswith(
            "community_ticker-"
        ):
            raise ValueError("user_content_collection_disabled")
        self.stage = "collect_" + view.kind
        self.detail_requested = 0
        self.json_responses = []
        self.goto(view.url)
        self.page.wait_for_timeout(300)
        if dom.client_error_visible(self.html()):
            self.loaded_at = 0
            raise SiteClientError("site_client_application_error")
        if dom.authentication_required(self.html()) or not self.authenticated():
            self.login()
            self.goto(view.url)
        self.page.get_by_role("button", name="프로필 메뉴", exact=True).wait_for(
            state="visible", timeout=15000
        )
        if view.source:
            self.click(self.page.get_by_role("tab", name=view.source, exact=True))
        if view.category:
            self.click(self.page.get_by_role("button", name=view.category, exact=True))
        if view.tab:
            self.click(self.page.get_by_role("button", name=view.tab, exact=True).first)
            self._toggle("정규장 기준", view.regular)
            self._toggle("위험주 제외", False)
        if view.month:
            self._calendar(view)
            self._toggle("전체", True)
        if view.kind == "news":
            self.page.evaluate("""() => {
              for(let e=document.querySelector('#news-tabpanel');e;e=e.parentElement) {
                if(e.scrollHeight > e.clientHeight) e.scrollTop=0;
              }
            }""")
            self.page.wait_for_timeout(300)
        # 첫 렌더링보다 데이터 응답이 늦는 경우 빈 성공으로 덮지 않도록 대기한다.
        rows = []
        for _ in range(10):
            rows = self._json_rows(view)
            if rows:
                break
            self.page.wait_for_timeout(500)
        if view.month and any(
            x.scheduled_date and x.scheduled_date[:7] != view.month for x in rows
        ):
            return Capture(
                stream=view.stream,
                page_url=view.url,
                observed_at=datetime.now(UTC),
                status="error",
                reason="calendar_range_mismatch",
                items=[],
            )
        if not rows:
            return Capture(
                stream=view.stream,
                page_url=view.url,
                observed_at=datetime.now(UTC),
                status="partial",
                reason="empty_or_unavailable",
                items=[],
            )
        if view.kind == "news" and not view.tab:

            def read_batch():
                batch = self._json_rows(view)
                return (
                    self._reports(batch) if view.stream == "news_reports" else self._details(batch)
                )

            rows, reason = accumulate(read_batch, self._scroll, max_steps=self.scroll_steps(view))
        else:
            reason = None
        if view.kind == "news" and any(
            x.detail_status != "ok" for x in rows if x.kind in ("news", "report")
        ):
            reason = "details_pending;" + (reason or "")
        if view.stream == "news_reports":
            self.pending_reports = any(item.attachment is None for item in rows)
        self.seen_streams.add(view.stream)
        return Capture(
            stream=view.stream,
            page_url=view.url,
            observed_at=datetime.now(UTC),
            items=rows,
            status="partial" if reason else "ok",
            reason=reason,
        )
