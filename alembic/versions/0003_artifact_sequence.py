"""add sequence column to artifacts for multi-media support

Revision ID: 0003_artifact_sequence
Revises: 0002_accounts_scheduled
Create Date: 2026-05-27 00:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_artifact_sequence"
down_revision: Union[str, None] = "0002_accounts_scheduled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the sequence column (default 0 for existing single-media artifacts)
    op.add_column(
        "artifacts",
        sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # Drop old unique constraint (account_id, platform, content_id, media_kind)
    op.drop_constraint(
        "uq_artifacts_account_platform_content_media",
        "artifacts",
        type_="unique",
    )

    # Create new unique constraint including sequence
    op.create_unique_constraint(
        "uq_artifacts_account_platform_content_media_seq",
        "artifacts",
        ["account_id", "platform", "content_id", "media_kind", "sequence"],
    )


def downgrade() -> None:
    # Drop new constraint
    op.drop_constraint(
        "uq_artifacts_account_platform_content_media_seq",
        "artifacts",
        type_="unique",
    )

    # Re-create old constraint
    op.create_unique_constraint(
        "uq_artifacts_account_platform_content_media",
        "artifacts",
        ["account_id", "platform", "content_id", "media_kind"],
    )

    # Drop sequence column
    op.drop_column("artifacts", "sequence")
