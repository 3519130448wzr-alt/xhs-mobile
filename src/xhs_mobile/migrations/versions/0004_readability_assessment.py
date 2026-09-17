"""Consecutive read recovery and audited quality reassessment; no data rewrite in DDL."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("consecutive_read_failures", "consecutive_no_progress"):
        op.add_column("tasks", sa.Column(name, sa.Integer(), server_default="0", nullable=False))
    op.create_table(
        "quality_assessment_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("revision", "entity_type", "entity_id", name="uq_quality_audit_entity"),
    )
    op.create_index(
        "ix_quality_assessment_audits_entity_id", "quality_assessment_audits", ["entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quality_assessment_audits_entity_id", table_name="quality_assessment_audits")
    op.drop_table("quality_assessment_audits")
    op.drop_column("tasks", "consecutive_no_progress")
    op.drop_column("tasks", "consecutive_read_failures")
