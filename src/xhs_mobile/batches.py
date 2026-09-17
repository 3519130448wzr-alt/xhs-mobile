"""Bounded single-device batches; this module never operates a phone or starts a run."""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from xhs_mobile.config import Policy, validate_target
from xhs_mobile.domain import utcnow
from xhs_mobile.models import Batch, BatchItem, Task
from xhs_mobile.repository import Repository, identifier, object_dict, serial_hash

BATCH_STATUSES = frozenset(
    {"pending", "running", "paused", "partial", "collected_awaiting_review"}
)


class BatchRepository:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.sessions = repository.sessions

    def create_batch(
        self,
        *,
        device_id: str,
        serial: str,
        session_ref: str,
        keywords: list[str],
        target: int,
        policy: dict,
    ) -> str:
        """Publish a batch and every ordered child in one transaction, or publish nothing."""
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20:
            raise ValueError("批次需要 1 至 20 个不同关键词")
        if any(not isinstance(keyword, str) or not keyword.strip() for keyword in keywords):
            raise ValueError("批次关键词不能为空")
        normalized = [keyword.strip() for keyword in keywords]
        if len(set(normalized)) != len(normalized):
            raise ValueError("批次关键词不能重复（含去除首尾空白后的重复）")
        validated_policy = Policy.model_validate(policy).model_dump()
        validate_target(target)
        if target > validated_policy["max_detail_visits"]:
            raise ValueError("每个关键词目标不能超过累计详情预算")
        for name, value in (("device_id", device_id), ("session_ref", session_ref)):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} 必须明确配置且不超过 128 个字符")
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("批次必须绑定明确的设备 serial")
        batch_id = identifier()
        binding = {
            "device_id": device_id,
            "serial_hash": serial_hash(serial),
            "session_ref": session_ref,
        }
        tasks = [
            Task(
                id=identifier(),
                **binding,
                keyword=keyword,
                target=target,
                policy=deepcopy(validated_policy),
                mode="search",
            )
            for keyword in normalized
        ]
        with self.sessions.begin() as session:
            session.add(Batch(id=batch_id, **binding))
            session.add_all(tasks)
            session.flush()
            session.add_all(
                BatchItem(batch_id=batch_id, position=position, task_id=task.id)
                for position, task in enumerate(tasks)
            )
        return batch_id

    @staticmethod
    def _batch(session: Session, batch_id: str) -> Batch:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise ValueError(f"没有批次 {batch_id}")
        return batch

    def batch(self, batch_id: str) -> Batch:
        with self.sessions() as session:
            return self._batch(session, batch_id)

    def task_ids(self, batch_id: str) -> list[str]:
        with self.sessions() as session:
            self._batch(session, batch_id)
            return list(
                session.scalars(
                    select(BatchItem.task_id)
                    .where(BatchItem.batch_id == batch_id)
                    .order_by(BatchItem.position)
                )
            )

    def status(self, batch_id: str | None = None) -> dict:
        # Reuse task reporting so observations never become verified unique notes by addition.
        with self.sessions() as session:
            query = select(Batch).order_by(Batch.created_at, Batch.id)
            if batch_id is not None:
                self._batch(session, batch_id)
                query = query.where(Batch.id == batch_id)
            batches = []
            for batch in session.scalars(query):
                record = object_dict(batch)
                items = session.scalars(
                    select(BatchItem)
                    .where(BatchItem.batch_id == batch.id)
                    .order_by(BatchItem.position)
                )
                record["tasks"] = [
                    {"id": item.task_id, "position": item.position} for item in items
                ]
                batches.append(record)
        # Read the immutable membership first: a concurrently created batch cannot reference
        # children omitted by an earlier task-status read.
        report = self.repository.status()
        task_records = {task["id"]: task for task in report["tasks"]}
        for batch in batches:
            batch["tasks"] = [
                {**task_records[item["id"]], "position": item["position"]}
                for item in batch["tasks"]
            ]
        return {
            "batches": batches,
            "policy_states": report["policy_states"],
            "real_device_acceptance": "not_inferred",
        }

    def update(self, batch_id: str, status: str, reason: str | None = None) -> None:
        if status not in BATCH_STATUSES:
            raise ValueError("未知批次状态")
        with self.sessions.begin() as session:
            batch = self._batch(session, batch_id)
            batch.status, batch.stop_reason, batch.updated_at = status, reason, utcnow()
            if status == "running":
                batch.pause_requested = False

    def request_pause(self, batch_id: str) -> None:
        """Persist intent and signal an active child; never reset budgets or acknowledge policy."""
        with self.sessions.begin() as session:
            batch = self._batch(session, batch_id)
            batch.pause_requested, batch.updated_at = True, utcnow()
            active = session.scalars(
                select(Task)
                .join(BatchItem, BatchItem.task_id == Task.id)
                .where(
                    BatchItem.batch_id == batch_id,
                    Task.status.in_(["running", "cooldown"]),
                )
            )
            for task in active:
                task.pause_requested, task.updated_at = True, utcnow()
