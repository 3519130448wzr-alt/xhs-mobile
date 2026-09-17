"""Explicit capture retry grants; additive and compatible with previous tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_retry_grants",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("step", sa.String(length=128), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("step = 'capture'", name="ck_retry_grant_capture_only"),
        sa.CheckConstraint("credits = 3", name="ck_retry_grant_three_credits"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_task_retry_grants_task_id", "task_retry_grants", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_retry_grants_task_id", table_name="task_retry_grants")
    op.drop_table("task_retry_grants")
