"""인증 정보가 들어갈 수 없는 수집 파일 계약."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pdf_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    pages: int = Field(ge=1)
    text_path: str | None = None
    text_chars: int = Field(default=0, ge=0)

    @field_validator("pdf_path", "text_path")
    @classmethod
    def local_attachment(cls, value):
        import re

        if value is not None and not re.fullmatch(r"attachments/[a-f0-9]{64}\.(pdf|txt)", value):
            raise ValueError("invalid_attachment_path")
        return value


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    title: str = ""
    body: str = ""
    author: str = ""
    publisher: str = ""
    source_url: str | None = None
    source_group: str | None = None
    categories: list[str] = Field(default_factory=list)
    is_breaking: bool | None = None
    report_date: str | None = None
    attachment: Attachment | None = None
    tickers: list[str] = Field(default_factory=list)
    displayed_time: str | None = None
    published_at: datetime | None = None
    scheduled_at: datetime | None = None
    scheduled_date: str | None = None
    rank: int | None = None
    price: str | None = None
    change_pct: str | None = None
    turnover: str | None = None
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    views: str | None = None
    likes: str | None = None
    comment_count: str | None = None
    parent_id: str | None = None
    kind: str = "item"
    detail_status: str = "not_requested"
    basis: str | None = None

    @field_validator("published_at", "scheduled_at")
    @classmethod
    def aware(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("시각에 시간대가 필요합니다")
            return value.astimezone(UTC)
        return value


class ViewInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = None
    category: str | None = None
    tab: str | None = None
    month: str | None = None
    requested_regular: bool | None = None


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    stream: str = Field(pattern=r"^(news|community|market|calendar)(_[a-z0-9-]+)*$")
    page_url: str
    observed_at: datetime
    status: Literal["ok", "partial", "error", "auth_required"] = "ok"
    reason: str | None = None
    items: list[Item]
    view: ViewInfo = Field(default_factory=ViewInfo)
    display_timezone: str = "Asia/Seoul"
    elapsed_s: float = Field(default=0, ge=0)
    target_interval_s: float = Field(default=30, ge=2)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("수집 시각에 시간대가 필요합니다")
        return value.astimezone(UTC)

    @field_validator("page_url")
    @classmethod
    def source_only(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in ("www.saveticker.com", "saveticker.com")
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("/news", "/community", "/watchlist", "/calendar")
        ):
            raise ValueError("허용된 SaveTicker 목록 URL이 아닙니다")
        return value

    @model_validator(mode="after")
    def nonempty_success(self):
        if self.status == "ok" and not self.items:
            raise ValueError("빈 목록은 성공으로 기록할 수 없습니다")
        if self.status != "ok" and not self.reason:
            raise ValueError("부분 수집·실패에는 사유가 필요합니다")
        return self
