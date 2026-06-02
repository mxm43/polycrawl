"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-24 00:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creators",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("creator_key", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creator_key"),
    )
    op.create_index("idx_creators_creator_key", "creators", ["creator_key"], unique=False)

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("account_type", sa.String(length=50), nullable=False),
        sa.Column("account_url", sa.String(length=500), nullable=False),
        sa.Column("platform_account_id", sa.String(length=255), nullable=True),
        sa.Column("account_alias", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "platform_account_id", name="uq_accounts_platform_platform_account_id"),
    )
    op.create_index("idx_accounts_creator_id", "accounts", ["creator_id"], unique=False)
    op.create_index("idx_accounts_platform_type", "accounts", ["platform", "account_type"], unique=False)
    op.create_index("idx_accounts_url", "accounts", ["account_url"], unique=False)
    op.create_index("idx_accounts_platform_id", "accounts", ["platform", "platform_account_id"], unique=False)

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("queue_key", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tasks_account_id", "tasks", ["account_id"], unique=False)
    op.create_index("idx_tasks_status", "tasks", ["status"], unique=False)
    op.create_index("idx_tasks_task_type", "tasks", ["task_type"], unique=False)
    op.create_index("idx_tasks_created_at", "tasks", [sa.text("created_at DESC")], unique=False)
    op.create_index("idx_tasks_queue_key", "tasks", ["queue_key"], unique=False)

    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(), nullable=True),
        sa.Column("items_fetched", sa.Integer(), nullable=True),
        sa.Column("items_downloaded", sa.Integer(), nullable=True),
        sa.Column("items_failed", sa.Integer(), nullable=True),
        sa.Column("items_skipped", sa.Integer(), nullable=True),
        sa.Column("bytes_downloaded", sa.BigInteger(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("log_entry_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "run_number", name="uq_task_runs_task_id_run_number"),
    )
    op.create_index("idx_task_runs_task_id", "task_runs", ["task_id"], unique=False)
    op.create_index("idx_task_runs_status", "task_runs", ["status"], unique=False)
    op.create_index("idx_task_runs_completed_at", "task_runs", [sa.text("completed_at DESC")], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("content_id", sa.String(length=255), nullable=False),
        sa.Column("media_kind", sa.String(length=50), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("publish_date", sa.DateTime(), nullable=True),
        sa.Column("download_date", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "platform", "content_id", "media_kind", name="uq_artifacts_account_platform_content_media"),
    )
    op.create_index("idx_artifacts_account_id", "artifacts", ["account_id"], unique=False)
    op.create_index("idx_artifacts_platform_content_id", "artifacts", ["platform", "content_id"], unique=False)
    op.create_index("idx_artifacts_download_date", "artifacts", [sa.text("download_date DESC")], unique=False)
    op.create_index("idx_artifacts_status", "artifacts", ["status"], unique=False)
    op.create_index("idx_artifacts_file_path", "artifacts", ["file_path"], unique=False)

    op.create_table(
        "live_statuses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("status_since", sa.DateTime(), nullable=False),
        sa.Column("current_recording_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recorded_seconds", sa.Integer(), nullable=True),
        sa.Column("recorded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_time", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id"),
    )
    op.create_index("idx_live_statuses_account_id", "live_statuses", ["account_id"], unique=False)
    op.create_index("idx_live_statuses_status", "live_statuses", ["status"], unique=False)

    op.create_table(
        "live_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("output_file_path", sa.String(length=500), nullable=True),
        sa.Column("total_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("segment_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_live_sessions_account_id", "live_sessions", ["account_id"], unique=False)
    op.create_index("idx_live_sessions_started_at", "live_sessions", [sa.text("started_at DESC")], unique=False)

    op.create_table(
        "config_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("config_content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_by", sa.String(length=100), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_config_versions_version_number", "config_versions", [sa.text("version_number DESC")], unique=False)
    op.create_index("idx_config_versions_changed_at", "config_versions", [sa.text("changed_at DESC")], unique=False)


def downgrade() -> None:
    op.drop_index("idx_config_versions_changed_at", table_name="config_versions")
    op.drop_index("idx_config_versions_version_number", table_name="config_versions")
    op.drop_table("config_versions")

    op.drop_index("idx_live_sessions_started_at", table_name="live_sessions")
    op.drop_index("idx_live_sessions_account_id", table_name="live_sessions")
    op.drop_table("live_sessions")

    op.drop_index("idx_live_statuses_status", table_name="live_statuses")
    op.drop_index("idx_live_statuses_account_id", table_name="live_statuses")
    op.drop_table("live_statuses")

    op.drop_index("idx_artifacts_file_path", table_name="artifacts")
    op.drop_index("idx_artifacts_status", table_name="artifacts")
    op.drop_index("idx_artifacts_download_date", table_name="artifacts")
    op.drop_index("idx_artifacts_platform_content_id", table_name="artifacts")
    op.drop_index("idx_artifacts_account_id", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("idx_task_runs_completed_at", table_name="task_runs")
    op.drop_index("idx_task_runs_status", table_name="task_runs")
    op.drop_index("idx_task_runs_task_id", table_name="task_runs")
    op.drop_table("task_runs")

    op.drop_index("idx_tasks_queue_key", table_name="tasks")
    op.drop_index("idx_tasks_created_at", table_name="tasks")
    op.drop_index("idx_tasks_task_type", table_name="tasks")
    op.drop_index("idx_tasks_status", table_name="tasks")
    op.drop_index("idx_tasks_account_id", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_accounts_platform_id", table_name="accounts")
    op.drop_index("idx_accounts_url", table_name="accounts")
    op.drop_index("idx_accounts_platform_type", table_name="accounts")
    op.drop_index("idx_accounts_creator_id", table_name="accounts")
    op.drop_table("accounts")

    op.drop_index("idx_creators_creator_key", table_name="creators")
    op.drop_table("creators")
