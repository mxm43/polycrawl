from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CreatorCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)


class CreatorResponse(BaseModel):
    creator_key: str
    display_name: str
    tags: list[str] = Field(default_factory=list)


class CreatorLinkResponse(BaseModel):
    platform: str
    account_type: str
    account_url: str
    account_alias: str | None = None
    last_updated_at: datetime | None = None


class CreatorPlatformGroupResponse(BaseModel):
    platform: str
    links: list[CreatorLinkResponse]


class CreatorSummaryResponse(BaseModel):
    creator_key: str
    display_name: str
    tags: list[str] = Field(default_factory=list)
    platforms: list[str]
    files_count: int
    works_count: int
    # Deprecated compatibility field: equals files_count.
    downloads_count: int
    total_bytes: int
    last_updated_at: datetime | None
    platform_groups: list[CreatorPlatformGroupResponse]


class AccountResponse(BaseModel):
    id: int
    creator_id: int
    creator_display_name: str | None = None
    platform: str
    account_type: str
    account_url: str
    account_alias: str | None = None
    scheduled: bool = True


class HealthResponse(BaseModel):
    status: str
    config: str = "ok"
    database: str = "unknown"


class TaskCreateRequest(BaseModel):
    account_id: int | None = None
    task_type: Literal["content_fetch", "live_record"]
    params: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0)


class TaskResponse(BaseModel):
    id: str
    account_id: int | None
    task_type: str
    status: str
    params: dict[str, Any]
    retry_count: int
    max_retries: int
    queue_key: str | None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class TaskRetryResponse(BaseModel):
    id: str
    retry_count: int
    status: str
    queue_key: str | None


class LiveStatusResponse(BaseModel):
    account_id: int
    creator_key: str | None = None
    display_name: str | None = None
    status: str
    status_since: datetime
    updated_at: datetime
    error_message: str | None = None


class StopLiveRecordResponse(BaseModel):
    account_id: int
    stopped: bool
    detail: str


class LinkCreateRequest(BaseModel):
    platform: str = Field(min_length=1)
    account_type: str = Field(default="profile", min_length=1)
    account_url: str = Field(min_length=1)
    account_alias: str | None = None


class ScheduleEntryResponse(BaseModel):
    type: str
    enabled: bool
    strategy: dict[str, Any] = Field(default_factory=lambda: {"use": "incremental"})
    tag_filter: list[str] | None = None
    start_at: str | None = None
    interval: str | None = None


class SchedulesResponse(BaseModel):
    tasks: list[ScheduleEntryResponse]


class ScheduleUpdateRequest(BaseModel):
    tasks: list[ScheduleEntryResponse]


class AccountScheduledRequest(BaseModel):
    scheduled: bool
