"""Chrome Native Messaging collector: local canonical data only, never browser requests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from trader.saveticker import dom, jsonfeed
from trader.saveticker.models import Capture, Item, ViewInfo
from trader.saveticker.reports import save_pdf
from trader.saveticker.service import View, build_views, lane_for
from trader.saveticker.store import Store, atomic_json

EXTENSION_ID = "gfchfnjghmjiidifohjglpodaakdphho"
ORIGIN = f"chrome-extension://{EXTENSION_ID}/"
MAX_FRAME = 64 * 1024 * 1024
MAX_SNAPSHOT = 4 * 1024 * 1024
MAX_REPLY = 1024 * 1024
LANES = {"news", "community", "market", "background"}
STATES = {"connecting", "collecting", "waiting_verification", "auth_required", "paused", "error"}
REASONS = {
    "verification_required",
    "login_required",
    "access_denied",
    "rate_limited",
    "selection_mismatch",
    "page_not_loaded",
    "collection_error",
    "scroll_limit",
    "extension_disconnected",
    "partial",
    "json_unverified",
    "empty",
    "pdf_failed",
}


def _now():
    return datetime.now(UTC)


def build_plan(now=None) -> dict:
    local = (now or _now()).astimezone(ZoneInfo("Asia/Seoul"))
    ordinal = local.year * 12 + local.month - 1

    def month(offset):
        year, index = divmod(ordinal + offset, 12)
        return f"{year:04}-{index + 1:02}"

    views = [
        View("news_all", "news", source="전체", category="전체", interval_s=30),
        View("news_reports", "news", source="SAVE 리포트", interval_s=900),
    ]
    views += build_views(month(-1), month(3))
    return {
        "protocol": 1,
        "views": [
            {
                "stream": v.stream,
                "kind": v.kind,
                "url": v.url,
                "tab": v.tab,
                "source": v.source,
                "category": v.category,
                "month": v.month,
                "regular": v.regular,
                "interval_s": max(30, v.interval_s),
            }
            for v in views
        ],
        "limits": {"max_scrolls": 2, "max_details": 2, "max_reports": 2},
    }


class InvalidMessage(Exception):
    """A fixed protocol error, with no user-supplied diagnostic content."""


def _reject(code):
    raise InvalidMessage(code)


def _clean_html(value):
    if not isinstance(value, str):
        _reject("invalid_message")
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.select("script,style,iframe,form,input,textarea,header,nav,[role=menu]"):
        node.decompose()
    for node in soup.find_all(True):
        node.attrs = {
            k: v
            for k, v in node.attrs.items()
            if k
            in {
                "class",
                "id",
                "role",
                "alt",
                "aria-label",
                "aria-current",
                "aria-selected",
                "aria-checked",
                "aria-pressed",
                "data-read",
                "data-breaking",
                "data-clickable",
                "data-stretched-link-card",
                "datetime",
            }
        }
    return str(soup)


def _body(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, str):
        return value[:200_000]
    if isinstance(value, list):
        return [_body(v, depth + 1) for v in value[:500]]
    if isinstance(value, dict):
        return {
            k: _body(v, depth + 1)
            for k, v in value.items()
            if k in {"text", "children", "content", "body"}
        }
    return None


def _project_row(row, kind):
    if not isinstance(row, dict):
        return {}
    fields = (
        {"id", "title", "created_at", "is_headline_only"}
        if kind in {"news", "detail"}
        else {"id", "title", "event_date", "event_date_only"}
        if kind == "calendar"
        else {"rank", "symbol", "nameKo", "labelKo", "price", "changePercent", "turnover"}
    )
    result = {
        k: v[:200_000] if isinstance(v, str) else v
        for k, v in row.items()
        if k in fields and (v is None or type(v) in (str, int, float, bool))
    }
    if kind in {"news", "detail", "calendar"}:
        result["content"] = _body(row.get("content"))
    if kind in {"news", "detail"}:
        for key in ("source", "channel"):
            value = row.get(key)
            if isinstance(value, str):
                result[key] = value[:200_000]
            elif isinstance(value, dict):
                result[key] = {"name": str(value.get("name", ""))[:200_000]}
        result["tickers"] = [
            {"symbol": x["symbol"][:256]}
            for x in row.get("tickers", [])[:500]
            if isinstance(x, dict) and isinstance(x.get("symbol"), str)
        ]
        result["content_labels"] = [
            {
                k: str(v)[:200_000]
                for k, v in x.items()
                if k in {"name", "label_name"} and isinstance(v, str)
            }
            for x in row.get("content_labels", [])[:500]
            if isinstance(x, dict)
        ]
        translated = row.get("translations", {})
        if isinstance(translated, dict):
            translated = translated.get("translated", {})
            if isinstance(translated, dict):
                translated = translated.get("ko_KR", {})
                if isinstance(translated, dict):
                    result["translations"] = {
                        "translated": {
                            "ko_KR": {
                                "title": str(translated.get("title", ""))[:200_000],
                                "content": _body(translated.get("content")),
                            }
                        }
                    }
    else:
        for key, allowed in (
            ("ticker", {"symbol", "nameKo"}),
            ("regular", {"price", "changePercent", "turnover"}),
        ):
            value = row.get(key)
            if isinstance(value, dict):
                result[key] = {
                    k: v for k, v in value.items() if k in allowed and type(v) in (str, int, float)
                }
    return result


def _project(data, kind):
    if not isinstance(data, dict):
        _reject("invalid_json")
    keys = (
        ("news_list",)
        if kind == "news"
        else ("events",)
        if kind == "calendar"
        else ("rankings", "items")
    )
    if kind == "detail":
        row = data.get("news", data.get("data", data))
        return _project_row(row, kind)
    return {
        key: [_project_row(x, kind) for x in data.get(key, [])[:500]]
        for key in keys
        if isinstance(data.get(key), list)
    }


class NativeSession:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._jobs = {}
        self._tickets = {}
        self._store = None
        self._plan = build_plan()

    @property
    def store(self):
        if self._store is None:
            self._store = Store(self.root / "data")
        return self._store

    def _heartbeat(self, state="collecting", reason=None, lane=None):
        stamp = _now().isoformat()
        row = {
            "backend": "extension",
            "pid": os.getpid(),
            "state": state,
            "updated_at": stamp,
            "at": stamp,
        }
        if reason:
            row["reason"] = reason
        runtime = self.root / "runtime"
        atomic_json(runtime / "bridge.json", row)
        atomic_json(runtime / "service.json", row)
        atomic_json(runtime / "runner.json", row)
        if lane:
            atomic_json(runtime / f"runner-{lane}.json", row)

    def disconnect(self):
        self._jobs.clear()
        self._tickets.clear()
        self._heartbeat("paused", "extension_disconnected")
        for lane in LANES:
            self._heartbeat("paused", "extension_disconnected", lane)

    def handle(self, message: dict) -> dict:
        identity = message.get("id") if isinstance(message, dict) else None
        kind = message.get("type") if isinstance(message, dict) else None
        # Do not echo arbitrarily large or structured attacker-controlled values.
        reply = {
            "id": identity if type(identity) is int else None,
            "type": kind if isinstance(kind, str) and len(kind) <= 32 else None,
        }
        try:
            if (
                not isinstance(message, dict)
                or type(identity) is not int
                or not isinstance(kind, str)
            ):
                _reject("invalid_message")
            result = self._handle(message)
            result = {**reply, "ok": True, **result}
            if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_REPLY:
                _reject("response_too_large")
            return result
        except InvalidMessage as exc:
            return {**reply, "ok": False, "error": str(exc)}
        except Exception:
            return {**reply, "ok": False, "error": "invalid_message"}

    def _handle(self, message):
        kind = message["type"]
        if kind == "hello":
            if type(message.get("protocol")) is not int or message["protocol"] != 1:
                _reject("unsupported_protocol")
            self._plan = build_plan()
            self._heartbeat("connecting")
            return {"type": "plan", **self._plan}
        if kind == "ping":
            self._heartbeat()
            return {"type": "pong"}
        if kind == "state":
            lane, state = message.get("lane"), message.get("state")
            if lane not in LANES or state not in STATES:
                _reject("invalid_message")
            reason = message.get("reason")
            self._heartbeat(state, reason if reason in REASONS else None, lane)
            return {"type": "state_saved"}
        if kind == "begin":
            stream = message.get("stream")
            view = next((v for v in self._plan["views"] if v["stream"] == stream), None)
            if view is None:
                _reject("unknown_stream")
            lane = self._lane(view)
            prior = self._jobs.get(lane)
            if prior and prior["expires_at"] > _now():
                _reject("lane_busy")
            job = {
                "job_id": secrets.token_hex(16),
                "view": view,
                "expires_at": _now() + timedelta(seconds=120),
            }
            self._jobs[lane] = job
            self._heartbeat(lane=lane)
            return {"type": "job", **job, "expires_at": job["expires_at"].isoformat()}
        if kind == "snapshot":
            return self._snapshot(message)
        if kind == "pdf_begin":
            return self._pdf_begin(message)
        if kind == "pdf":
            return self._pdf(message)
        _reject("invalid_message")

    @staticmethod
    def _lane(view):
        return lane_for(View(view["stream"], view["kind"]))

    def _snapshot(self, message):
        if len(json.dumps(message, ensure_ascii=False).encode()) > MAX_SNAPSHOT:
            _reject("snapshot_too_large")
        view = next((v for v in self._plan["views"] if v["stream"] == message.get("stream")), None)
        if view is None:
            _reject("unknown_stream")
        lane = self._lane(view)
        job = self._jobs.get(lane)
        if (
            not job
            or job["expires_at"] <= _now()
            or job["view"] != view
            or message.get("job_id") != job["job_id"]
        ):
            _reject("invalid_job")
        if message.get("page_url") != view["url"]:
            _reject("invalid_url")
        status = message.get("status", "partial")
        regular_equivalent = False
        if status not in {"ok", "partial", "error", "auth_required"}:
            _reject("invalid_message")
        if status not in {"error", "auth_required"}:
            selection = message.get("selection")
            if not isinstance(selection, dict) or selection.get("loaded") is not True:
                _reject("selection_mismatch")
            for key in ("source", "category", "tab", "month"):
                if view.get(key) is not None and selection.get(key) != view[key]:
                    _reject("selection_mismatch")
            regular_equivalent = (
                view["kind"] == "community"
                and selection.get("regular") is True
                and selection.get("regular_disabled") is True
                and selection.get("regular_equivalent") is True
                and selection.get("regular_equivalence_label")
                == "지금은 실시간과 정규장 기준이 같아요"
            )
            if view["tab"] and (
                type(selection.get("regular")) is not bool
                or (selection["regular"] != view["regular"] and not regular_equivalent)
            ):
                _reject("selection_mismatch")
        items = []
        reason = message.get("reason")
        if reason == "selection_unverified":
            reason = "selection_mismatch"
        reason = reason if reason in REASONS else "partial"
        responses = message.get("json_responses", [])
        if not isinstance(responses, list) or len(responses) > 500:
            _reject("invalid_message")
        for response in responses:
            if not isinstance(response, dict):
                _reject("invalid_message")
            if (
                response.get("job_id") == message["job_id"]
                and self._route(response.get("url", ""))
                and response.get("status") in {401, 403, 429}
            ):
                status = "auth_required"
                reason = {401: "login_required", 403: "access_denied", 429: "rate_limited"}[
                    response["status"]
                ]
                break
        if status not in {"error", "auth_required"}:
            html = _clean_html(message.get("html", ""))
            items = dom.extract(html, view["stream"], month=view["month"])
            # Virtualised lists and bounded scrolling do not establish completeness.
            status = "partial"
            items = self._json_items(message, view, items)
            if regular_equivalent:
                items = [
                    item.model_copy(
                        update={
                            "basis": "; ".join(
                                filter(None, [item.basis, "지금은 실시간과 정규장 기준이 같아요"])
                            )
                        }
                    )
                    if item.kind == "quote"
                    else item
                    for item in items
                ]
            details = message.get("details", [])
            if not isinstance(details, list) or len(details) > 2:
                _reject("invalid_message")
            for detail in details:
                if not isinstance(detail, dict):
                    _reject("invalid_message")
                items = [
                    dom.news_detail(_clean_html(detail.get("html", "")), x)
                    if x.kind == "news" and x.title == detail.get("title")
                    else x
                    for x in items
                ]
            items = [x for x in items if x.kind in {"news", "quote", "event", "report"}]
            if view["stream"] == "news_all":
                previous = {}
                for row in self.store.query(asof=_now(), limit=10000):
                    old = row["item"]
                    if old.get("detail_status") == "ok" and old.get("body"):
                        previous.setdefault(old["id"], Item.model_validate(old))
                items = [
                    item.model_copy(
                        update={
                            key: getattr(previous[item.id], key)
                            for key in ("body", "published_at", "detail_status")
                        }
                    )
                    if item.id in previous and not item.body
                    else item
                    for item in items
                ]
            if view["stream"] == "news_reports":
                items = [self._cached_report(x) for x in items]
        capture = Capture(
            stream=view["stream"],
            page_url=view["url"],
            observed_at=_now(),
            status=status,
            reason=reason,
            items=items,
            view=ViewInfo(
                source=view["source"],
                category=view["category"],
                tab=view["tab"],
                month=view["month"],
                requested_regular=view["regular"] if view["tab"] else None,
            ),
            target_interval_s=view["interval_s"],
        )
        result = self.store.save(capture)
        del self._jobs[lane]
        phase = status if status in {"auth_required", "error"} else "collecting"
        self._heartbeat(phase, reason=reason, lane=lane)
        if view["stream"] == "news_reports":
            result["report_items"] = [
                {"id": x.id, "title": x.title, "attachment_ok": x.attachment is not None}
                for x in items
            ]
        return {"type": "saved", **result}

    @staticmethod
    def _route(url):
        if not isinstance(url, str):
            return None
        parsed = urlsplit(url)
        if parsed.netloc != "www.saveticker.com":
            return None
        return jsonfeed.route(url)

    def _json_items(self, message, view, fallback):
        responses = message.get("json_responses", [])
        if not isinstance(responses, list) or len(responses) > 500:
            _reject("invalid_message")
        expected = (
            "news"
            if view["stream"] == "news_all"
            else "calendar"
            if view["kind"] == "calendar"
            else "top5"
            if view["stream"] == "community_top5"
            else "index"
            if "_index_" in view["stream"]
            else "popular"
        )
        items = None
        details = []
        for response in responses:
            if not isinstance(response, dict):
                _reject("invalid_message")
            route = self._route(response.get("url", ""))
            if response.get("job_id") != message["job_id"] or response.get("status") != 200:
                continue
            if route == "detail" and view["stream"] == "news_all":
                details.append(
                    jsonfeed.news_item(_project(response.get("data"), route), detail=True)
                )
            elif route == expected and view["stream"] != "news_reports":
                data = _project(response.get("data"), route)
                if view["regular"]:
                    rows = data.get("rankings", data.get("items", []))
                    if not all(isinstance(x.get("regular"), dict) and x["regular"] for x in rows):
                        continue
                extracted = jsonfeed.extract(
                    data, view["stream"], month=view["month"], regular=view["regular"]
                )
                items = {x.id: x for x in (list(items.values()) if items else []) + extracted}
        result = list(items.values()) if items else fallback
        detailed = {x.id: x for x in details[:2]}
        return [detailed.get(x.id, x) for x in result]

    def _reports(self):
        path = self.root / "data/latest/news_reports.json"
        try:
            return Capture.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None

    def _attachment_valid(self, attachment):
        if attachment is None:
            return False
        path = self.root / "data" / attachment.pdf_path
        try:
            return (
                not path.is_symlink()
                and path.is_file()
                and path.stat().st_size == attachment.size_bytes
                and hashlib.sha256(path.read_bytes()).hexdigest() == attachment.sha256
            )
        except OSError:
            return False

    def _cached_report(self, item):
        reports = self._reports()
        if reports:
            prior = next((x for x in reports.items if x.id == item.id), None)
            if prior and self._attachment_valid(prior.attachment):
                path = self.root / "data" / prior.attachment.pdf_path
                if _now().timestamp() - path.stat().st_mtime < 86400:
                    return prior
        return item

    def _pdf_begin(self, message):
        reports = self._reports()
        item_id = message.get("item_id")
        item = next((x for x in reports.items if x.id == item_id), None) if reports else None
        if item is None:
            _reject("unknown_report")
        cached = self._cached_report(item)
        if cached.attachment and self._attachment_valid(cached.attachment):
            path = self.root / "data" / cached.attachment.pdf_path
            if _now().timestamp() - path.stat().st_mtime < 86400:
                _reject("pdf_cached")
        for ticket in self._tickets.values():
            if ticket["expires_at"] > _now():
                _reject("pdf_busy")
        ticket = secrets.token_hex(16)
        name = f"{secrets.token_hex(16)}-{int(_now().timestamp() * 1000):013}.pdf"
        self._tickets = {
            ticket: {
                "item_id": item_id,
                "download_name": name,
                "issued_at": _now(),
                "expires_at": _now() + timedelta(seconds=120),
            }
        }
        return {"type": "pdf_job", "ticket": ticket, "download_name": name}

    def _pdf(self, message):
        ticket = self._tickets.get(message.get("ticket"))
        if (
            not ticket
            or ticket["expires_at"] <= _now()
            or ticket["item_id"] != message.get("item_id")
            or ticket["download_name"] != message.get("download_name")
        ):
            _reject("invalid_pdf_ticket")
        name = message["download_name"]
        if not re.fullmatch(r"[a-f0-9]{32}-\d{13}\.pdf", name):
            _reject("invalid_pdf_path")
        directory = Path.home() / "Downloads" / "SaveTicker"
        source = directory / name
        try:
            if (
                any(x.is_symlink() for x in [source, directory, directory.parent])
                or source.resolve().parent != directory.resolve()
                or not source.is_file()
                or source.stat().st_mtime < ticket["issued_at"].timestamp() - 1
                or source.stat().st_mtime > _now().timestamp() + 1
            ):
                _reject("invalid_pdf_path")
            attachment = save_pdf(source, self.root / "data")
        except OSError:
            _reject("invalid_pdf_path")
        except ValueError:
            _reject("invalid_pdf")
        reports = self._reports()
        if not reports or not any(x.id == message["item_id"] for x in reports.items):
            _reject("unknown_report")
        items = [
            x.model_copy(
                update={
                    "attachment": attachment,
                    "detail_status": "ok" if attachment.text_chars else "pdf_only",
                }
            )
            if x.id == message["item_id"]
            else x
            for x in reports.items
        ]
        self.store.save(reports.model_copy(update={"items": items, "observed_at": _now()}))
        del self._tickets[message["ticket"]]
        self._heartbeat(lane="background")
        return {
            "type": "pdf_saved",
            "item_id": message["item_id"],
            "attachment": attachment.model_dump(mode="json"),
        }


def _write(output, reply):
    data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
    output.write(struct.pack("=I", len(data)) + data)
    output.flush()


def _read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        part = stream.read(length - len(chunks))
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


def serve(root: Path, extension_id: str, origin: str, stdin=None, stdout=None):
    """Run one connection, holding both collector locks until EOF."""
    from trader.daemon import Lock

    input_stream = stdin if stdin is not None else sys.stdin.buffer
    output = stdout if stdout is not None else sys.stdout.buffer
    if extension_id != EXTENSION_ID or origin != ORIGIN:
        _write(output, {"id": None, "type": None, "ok": False, "error": "invalid_origin"})
        return
    runtime = Path(root) / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    bridge_lock, service_lock = Lock(runtime / "bridge-host.lock"), Lock(runtime / "service.lock")
    if not bridge_lock.acquire():
        _write(output, {"id": None, "type": None, "ok": False, "error": "host_busy"})
        return
    session = None
    service_acquired = False
    try:
        if not service_lock.acquire():
            _write(output, {"id": None, "type": None, "ok": False, "error": "collector_busy"})
            return
        service_acquired = True
        session = NativeSession(root)
        session._heartbeat("connecting")
        while True:
            header = _read_exact(input_stream, 4)
            if not header:
                break
            if len(header) != 4:
                _write(output, {"id": None, "type": None, "ok": False, "error": "invalid_frame"})
                break
            size = struct.unpack("=I", header)[0]
            if not 0 < size <= MAX_FRAME:
                _write(output, {"id": None, "type": None, "ok": False, "error": "invalid_frame"})
                break
            payload = _read_exact(input_stream, size)
            try:
                if len(payload) != size:
                    raise ValueError
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeError):
                _write(output, {"id": None, "type": None, "ok": False, "error": "invalid_frame"})
                break
            _write(output, session.handle(message))
    except Exception:
        _write(output, {"id": None, "type": None, "ok": False, "error": "host_error"})
    finally:
        try:
            if session is not None:
                try:
                    session.disconnect()
                except Exception:
                    pass
        finally:
            try:
                if service_acquired:
                    service_lock.release()
            finally:
                bridge_lock.release()
