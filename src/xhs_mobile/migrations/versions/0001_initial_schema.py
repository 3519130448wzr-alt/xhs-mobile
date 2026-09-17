"""Frozen initial schema for tasks, runs, observations, events, and policy state.

This revision intentionally declares columns and constraints explicitly. Future
model changes require new revisions; replaying this one must not import models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_states",
        sa.Column("scope", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_used", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("serial_hash", sa.String(length=64), nullable=False),
        sa.Column("session_ref", sa.String(length=128), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("detail_visits", sa.Integer(), nullable=False),
        sa.Column("list_swipes", sa.Integer(), nullable=False),
        sa.Column("no_progress", sa.Integer(), nullable=False),
        sa.Column("retry_counts", sa.JSON(), nullable=False),
        sa.Column("pending_step", sa.String(length=128), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_events_task_id"), "events", ["task_id"], unique=False)
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_runs_task_id"), "runs", ["task_id"], unique=False)
    op.create_table(
        "observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("session_ref", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.String(length=256), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("identity_source", sa.String(length=80), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("app_version", sa.String(length=100), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_verdict", sa.String(length=16), nullable=True),
        sa.Column("reviewer", sa.String(length=128), nullable=True),
        sa.Column("review_identity", sa.String(length=300), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "note_id", name="uq_task_note"),
    )
    op.create_index(
        op.f("ix_observations_fingerprint"), "observations", ["fingerprint"], unique=False
    )
    op.create_index(op.f("ix_observations_note_id"), "observations", ["note_id"], unique=False)
    op.create_index(op.f("ix_observations_task_id"), "observations", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_observations_task_id"), table_name="observations")
    op.drop_index(op.f("ix_observations_note_id"), table_name="observations")
    op.drop_index(op.f("ix_observations_fingerprint"), table_name="observations")
    op.drop_table("observations")
    op.drop_index(op.f("ix_runs_task_id"), table_name="runs")
    op.drop_table("runs")
    op.drop_index(op.f("ix_events_task_id"), table_name="events")
    op.drop_table("events")
    op.drop_table("tasks")
    op.drop_table("policy_states")
