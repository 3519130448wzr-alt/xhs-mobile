"""Frozen additive schema for ordered batches bound to one device and session."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("serial_hash", sa.String(length=64), nullable=False),
        sa.Column("session_ref", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "batch_items",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("batch_id", "position"),
        sa.UniqueConstraint("task_id", name="uq_batch_item_task"),
    )


def downgrade() -> None:
    op.drop_table("batch_items")
    op.drop_table("batches")
