"""API job lifecycle and correlated enrichment. This module never makes HTTP calls."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from trader.saveticker import api_contract, jsonfeed
from trader.saveticker.models import Capture, Item, ViewInfo


class ApiError(Exception):
    """Fixed diagnostic code only."""


def fail(code):
    raise ApiError(code)


def now():
    return datetime.now(UTC)


def request(path, query, kind, *, max_bytes=api_contract.MAX_BYTES):
    return {
        "method": "GET",
        "url": api_contract.ORIGIN + path + ("?" + urlencode(query) if query else ""),
        "kind": kind,
        "max_bytes": max_bytes,
    }


def digest(response):
    return hashlib.sha256(
        json.dumps(response, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class BrowserApiSession:
    def __init__(self, owner):
        self.owner = owner
        self.group_id = None
        self.context_job = None
        self.report_sources = {}

    def clear(self):
        self.group_id = None
        self.context_job = None
        self.report_sources.clear()

    def descriptor(self, view):
        return api_contract.descriptor(view, group_id=self.group_id)

    def view(self, stream):
        view = next((v for v in self.owner._plan["views"] if v["stream"] == stream), None)
        if not view:
            fail("unknown_stream")
        return view

    def job(self, message):
        view = self.view(message.get("stream"))
        lane = self.owner._lane(view)
        job = self.owner._jobs.get(lane)
        if (
            not job
            or job.get("backend") != "api"
            or job["job_id"] != message.get("job_id")
            or job["view"] != view
            or job["expires_at"] <= now()
        ):
            fail("invalid_job")
        return view, lane, job

    @staticmethod
    def wire(job):
        return {**job, "expires_at": job["expires_at"].isoformat()}

    def handle(self, message):
        if len(json.dumps(message, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            fail("snapshot_too_large")
        kind = message["type"]
        if kind == "api_context_begin":
            self.context_job = {
                "job_id": secrets.token_hex(16),
                "expires_at": now() + timedelta(seconds=120),
                "request": request("/api/stocks/api/v1/watchlist/groups", {}, "groups"),
            }
            return self.wire(self.context_job)
        if kind == "api_context":
            job = self.context_job
            if not job or job["job_id"] != message.get("job_id") or job["expires_at"] <= now():
                fail("invalid_job")
            try:
                data = api_contract.validate_response(job["request"], message.get("response"))
                groups = data.get("groups")
                if (
                    not isinstance(groups, list)
                    or not 0 < len(groups) <= 128
                    or any(not isinstance(g, dict) for g in groups)
                ):
                    fail("api_context_unverified")
                defaults = [g for g in groups if g.get("isDefault") is True]
                if (
                    len(defaults) != 1
                    or type(defaults[0].get("groupId")) is not int
                    or not 0 < defaults[0]["groupId"] < 2**53
                ):
                    fail("api_context_unverified")
                self.group_id = defaults[0]["groupId"]
                self.context_job = None
                return {"type": "context_ready"}
            except (ValueError, TypeError):
                fail("api_context_unverified")
        if kind == "api_begin":
            view = self.view(message.get("stream"))
            contract = self.descriptor(view)
            if contract is None:
                fail("api_contract_unverified")
            lane = self.owner._lane(view)
            prior = self.owner._jobs.get(lane)
            if prior and prior["expires_at"] > now():
                fail("lane_busy")
            job = {
                "job_id": secrets.token_hex(16),
                "view": view,
                "expires_at": now() + timedelta(seconds=120),
                "backend": "api",
                "request": contract,
            }
            self.owner._jobs[lane] = job
            self.owner._heartbeat(lane=lane)
            return {"type": "job", **self.wire(job)}
        if kind == "api_pdf_begin":
            return self.pdf_begin(message)
        view, lane, job = self.job(message)
        if kind == "api_cancel":
            del self.owner._jobs[lane]
            blocked = message.get("reason") == "verification_required"
            self.owner._heartbeat(
                "waiting_verification" if blocked else "paused",
                "verification_required" if blocked else "collection_error",
                lane,
            )
            return {"type": "cancelled"}
        try:
            items = api_contract.parse_snapshot(
                view, message.get("response"), group_id=self.group_id
            )
            if kind == "api_prepare":
                return self.prepare(job, message["response"], items)
            if kind != "api_snapshot":
                fail("invalid_message")
            if "base_hash" in job and digest(message["response"]) != job["base_hash"]:
                fail("api_response_unverified")
            items, pdf_items = self.enrich(job, message.get("details", []), items)
        except (ValueError, TypeError, KeyError, AttributeError):
            fail("api_response_unverified")
        if view["stream"] == "news_all":
            previous = {
                r["item"]["id"]: Item.model_validate(r["item"])
                for r in self.owner.store.query(asof=now(), limit=10000)
                if r["item"].get("body") and r["item"].get("detail_status") == "ok"
            }
            items = [
                i.model_copy(
                    update={
                        k: getattr(previous[i.id], k)
                        for k in ("body", "detail_status", "published_at")
                    }
                )
                if i.id in previous and not i.body
                else i
                for i in items
            ]
        if view["stream"] == "news_reports":
            items = [self.owner._cached_report(i) for i in items]
        capture = Capture(
            stream=view["stream"],
            page_url=view["url"],
            observed_at=now(),
            status="partial",
            reason="partial",
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
        result = self.owner.store.save(capture)
        del self.owner._jobs[lane]
        self.owner._heartbeat("collecting", "partial", lane)
        return {"type": "saved", **result, "pdf_items": pdf_items}

    def prepare(self, job, response, items):
        if job["view"]["stream"] not in {"news_all", "news_reports"}:
            return {"requests": []}
        if "base_hash" in job:
            fail("api_already_prepared")
        reports = job["view"]["stream"] == "news_reports"
        previous = (
            {r["item"]["id"]: r["item"] for r in self.owner.store.query(asof=now(), limit=10000)}
            if not reports
            else {}
        )
        rows = response["data"]["reports" if reports else "news_list"]
        children = []
        for row, item in zip(rows, items, strict=True):
            if reports:
                if self.owner._cached_report(item).attachment:
                    continue
            elif (
                item.detail_status == "ok"
                or previous.get(item.id, {}).get("detail_status") == "ok"
                and previous[item.id].get("body")
            ):
                continue
            source = str(row["id"])
            if not re.fullmatch(r"\d{1,20}" if reports else r"[A-Za-z0-9_-]{1,128}", source):
                fail("api_source_unverified")
            child = {
                "item_id": item.id,
                "source_id": source,
                "job_id": job["job_id"],
                "expires_at": job["expires_at"].isoformat(),
                "request": request(
                    "/api/report/detail" if reports else "/api/news/detail",
                    {"id": source},
                    "report_detail" if reports else "news_detail",
                ),
            }
            children.append(child)
            if len(children) >= (1 if reports else 2):
                break
        job["base_hash"] = digest(response)
        job["children"] = children
        return {"requests": children}

    def enrich(self, job, details, items):
        children = job.get("children", [])
        if not isinstance(details, list) or len(details) > len(children):
            fail("api_detail_unverified")
        mapped = {c["request"]["url"]: c for c in children}
        seen, updates, pdf_items = set(), {}, []
        base = {i.id: i for i in items}
        for response in details:
            if (
                not isinstance(response, dict)
                or response.get("url") not in mapped
                or response["url"] in seen
            ):
                fail("api_detail_unverified")
            seen.add(response["url"])
            child = mapped[response["url"]]
            data = api_contract.validate_response(child["request"], response)
            item = base[child["item_id"]]
            if child["request"]["kind"] == "news_detail":
                if str(data.get("id")) != child["source_id"]:
                    fail("api_detail_unverified")
                enriched = jsonfeed.news_item(data, detail=True)
                if enriched.id != item.id or enriched.detail_status != "ok":
                    fail("api_detail_unverified")
                updates[item.id] = enriched
            else:
                report = data.get("report")
                if (
                    not isinstance(report, dict)
                    or str(report.get("id")) != child["source_id"]
                    or report.get("title") != item.title
                    or not re.fullmatch(
                        r"/api/uploads/documents/\d{1,20}", str(report.get("pdf_url", ""))
                    )
                ):
                    fail("api_pdf_unverified")
                self.report_sources[item.id] = {
                    "path": report["pdf_url"],
                    "expires_at": job["expires_at"],
                }
                pdf_items.append({"id": item.id})
        return [updates.get(i.id, i) for i in items], pdf_items

    def pdf_begin(self, message):
        source = self.report_sources.get(message.get("item_id"))
        if not source or source["expires_at"] <= now():
            fail("api_pdf_unverified")
        result = self.owner._pdf_begin(message)
        ticket = self.owner._tickets[result["ticket"]]
        return {
            **result,
            "job_id": result["ticket"],
            "expires_at": ticket["expires_at"].isoformat(),
            "request": request(
                "/api/report/download", {"url": source["path"]}, "pdf", max_bytes=25 * 1024 * 1024
            ),
        }
