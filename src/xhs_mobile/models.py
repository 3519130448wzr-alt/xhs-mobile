"""Portable SQLAlchemy models; production uses PostgreSQL and Alembic migrations."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from xhs_mobile.domain import utcnow


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128))
    serial_hash: Mapped[str] = mapped_column(String(64))
    session_ref: Mapped[str] = mapped_column(String(128))
    keyword: Mapped[str] = mapped_column(Text)
    target: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20), default="search")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    detail_visits: Mapped[int] = mapped_column(Integer, default=0)
    list_swipes: Mapped[int] = mapped_column(Integer, default=0)
    no_progress: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_read_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    consecutive_no_progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    pending_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRetryGrant(Base):
    """Explicit, idempotent read recovery; never changes cumulative failures."""

    __tablename__ = "task_retry_grants"
    __table_args__ = (
        CheckConstraint("step = 'capture'", name="ck_retry_grant_capture_only"),
        CheckConstraint("credits = 3", name="ck_retry_grant_three_credits"),
    )

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    step: Mapped[str] = mapped_column(String(128), default="capture")
    credits: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QualityAssessmentAudit(Base):
    """Immutable pre-upgrade assessment; original observations and evidence remain intact."""

    __tablename__ = "quality_assessment_audits"
    __table_args__ = (
        UniqueConstraint("revision", "entity_type", "entity_id", name="uq_quality_audit_entity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    entity_type: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSON)
    after: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128))
    serial_hash: Mapped[str] = mapped_column(String(64))
    session_ref: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("task_id", name="uq_batch_item_task"),)

    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    profile_hash: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="running")


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("task_id", "note_id", name="uq_task_note"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    device_id: Mapped[str] = mapped_column(String(128))
    session_ref: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32), default="xiaohongshu")
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    note_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    eligible: Mapped[bool] = mapped_column(Boolean)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    app_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_identity: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyState(Base):
    __tablename__ = "policy_states"

    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    status: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_used: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
