from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Creator(Base):
    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    account_url: Mapped[str] = mapped_column(String(500), nullable=False)
    platform_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("platform", "platform_account_id", name="uq_accounts_platform_platform_account_id"),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    queue_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    items_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_downloaded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_downloaded: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    log_entry_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("task_id", "run_number", name="uq_task_runs_task_id_run_number"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    content_id: Mapped[str] = mapped_column(String(255), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publish_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    download_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="completed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "platform", "content_id", "media_kind", "sequence", name="uq_artifacts_account_platform_content_media_seq"),
    )


class LiveStatus(Base):
    __tablename__ = "live_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    status_since: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    current_recording_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recorded_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    output_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    segment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfigVersion(Base):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config_content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


Index("idx_creators_creator_key", Creator.creator_key)
Index("idx_accounts_creator_id", Account.creator_id)
Index("idx_accounts_platform_type", Account.platform, Account.account_type)
Index("idx_accounts_url", Account.account_url)
Index("idx_accounts_platform_id", Account.platform, Account.platform_account_id)
Index("idx_tasks_account_id", Task.account_id)
Index("idx_tasks_status", Task.status)
Index("idx_tasks_task_type", Task.task_type)
Index("idx_tasks_created_at", Task.created_at.desc())
Index("idx_tasks_queue_key", Task.queue_key)
Index("idx_task_runs_task_id", TaskRun.task_id)
Index("idx_task_runs_status", TaskRun.status)
Index("idx_task_runs_completed_at", TaskRun.completed_at.desc())
Index("idx_artifacts_account_id", Artifact.account_id)
Index("idx_artifacts_platform_content_id", Artifact.platform, Artifact.content_id)
Index("idx_artifacts_download_date", Artifact.download_date.desc())
Index("idx_artifacts_status", Artifact.status)
Index("idx_artifacts_file_path", Artifact.file_path)
Index("idx_live_statuses_account_id", LiveStatus.account_id)
Index("idx_live_statuses_status", LiveStatus.status)
Index("idx_live_sessions_account_id", LiveSession.account_id)
Index("idx_live_sessions_started_at", LiveSession.started_at.desc())
Index("idx_config_versions_version_number", ConfigVersion.version_number.desc())
Index("idx_config_versions_changed_at", ConfigVersion.changed_at.desc())
