"""Short transactions; UI operations must never execute inside these transactions."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from xhs_mobile.config import validate_target
from xhs_mobile.domain import ParsedNote, Snapshot, utcnow
from xhs_mobile.models import Event, Observation, PolicyState, Run, Task, TaskRetryGrant

READ_STEPS = {"capture", "read_state"}
READ_ANOMALY_THRESHOLD = 10


def identifier() -> str:
    return str(uuid4())


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def serial_hash(serial: str) -> str:
    return hashlib.sha256(serial.encode()).hexdigest()


def object_dict(obj: Any) -> dict:
    result = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        result[column.name] = aware(value).isoformat() if isinstance(value, datetime) else value
    return result


class Repository:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def connect(cls, url: str) -> "Repository":
        return cls(create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10}))

    def create_task(
        self,
        *,
        device_id: str,
        serial: str,
        session_ref: str,
        keyword: str,
        target: int,
        policy: dict,
        mode: str = "search",
    ) -> Task:
        target = validate_target(target)
        if not keyword.strip():
            raise ValueError("keyword must be nonempty and target must be positive")
        task = Task(
            id=identifier(),
            device_id=device_id,
            serial_hash=serial_hash(serial),
            session_ref=session_ref,
            keyword=keyword.strip(),
            target=target,
            policy=policy,
            mode=mode,
        )
        with self.sessions.begin() as session:
            session.add(task)
        return task

    def task(self, task_id: str) -> Task:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"没有任务 {task_id}")
            return task

    def start_run(self, task_id: str, profile_hash: str) -> str:
        run_id = identifier()
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            # Only an explicitly continued threshold pause releases its reminder.
            # A crashed/in-flight run retains both streaks across process restarts.
            if task.status == "paused" and task.stop_reason in {
                "consecutive_read_failures:10", "consecutive_no_progress:10",
            }:
                session.add(Event(id=identifier(), task_id=task_id, run_id=None,
                                  kind="read_anomaly_resume", detail={
                                      "reason": task.stop_reason,
                                      "consecutive_read_failures": task.consecutive_read_failures,
                                      "consecutive_no_progress": task.consecutive_no_progress,
                                  }))
                if task.stop_reason == "consecutive_read_failures:10":
                    task.consecutive_read_failures = 0
                else:
                    task.consecutive_no_progress = 0
            self._settle_pending_read(task)
            for previous in session.scalars(
                select(Run).where(Run.task_id == task_id, Run.status.in_(["running", "cooldown"]))
            ):
                previous.status = "interrupted"
                previous.ended_at = utcnow()
            session.add(Run(id=run_id, task_id=task_id, profile_hash=profile_hash))
            task.status = "running"
            task.stop_reason = None
            task.pause_requested = False
            task.updated_at = utcnow()
        return run_id

    @staticmethod
    def _task(session: Session, task_id: str) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"没有任务 {task_id}")
        return task

    def event(self, task_id: str | None, run_id: str | None, kind: str, detail: dict) -> None:
        with self.sessions.begin() as session:
            session.add(
                Event(
                    id=identifier(),
                    task_id=task_id,
                    run_id=run_id,
                    kind=kind,
                    detail=detail,
                )
            )

    def finish(self, task_id: str, run_id: str, status: str, reason: str) -> None:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            task.status, task.stop_reason, task.updated_at = status, reason, utcnow()
            run = session.get(Run, run_id)
            if run:
                run.status, run.ended_at = status, utcnow()
            session.add(
                Event(
                    id=identifier(),
                    task_id=task_id,
                    run_id=run_id,
                    kind="run_finished",
                    detail={"status": status, "reason": reason},
                )
            )

    def request_pause(self, task_id: str) -> None:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            task.pause_requested = True
            task.updated_at = utcnow()

    def raise_target(self, task_id: str, target: int) -> None:
        target = validate_target(target)
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            if target <= task.target:
                raise ValueError("补采目标必须大于原目标；不能减少或重置任务")
            if target > task.policy["max_detail_visits"]:
                raise ValueError("目标超过本任务累计详情预算；请另建有明确预算的任务")
            task.target, task.updated_at = target, utcnow()

    def consume(self, task_id: str, counter: str, limit: int) -> bool:
        if counter not in {"detail_visits", "list_swipes"}:
            raise ValueError("unknown budget counter")
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            current = getattr(task, counter)
            if current >= limit:
                return False
            setattr(task, counter, current + 1)
            task.updated_at = utcnow()
            return True

    def consume_retry(self, task_id: str, step: str, limit: int) -> bool:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            counts = dict(task.retry_counts)
            if counts.get(step, 0) >= limit:
                return False
            counts[step] = counts.get(step, 0) + 1
            task.retry_counts = counts
            task.updated_at = utcnow()
            return True

    @staticmethod
    def _read_credits(session: Session, task_id: str, step: str = "capture") -> int:
        if step != "capture":
            return 0
        return int(session.scalar(select(func.coalesce(func.sum(TaskRetryGrant.credits), 0)).where(
            TaskRetryGrant.task_id == task_id, TaskRetryGrant.step == "capture",
        )) or 0)

    @staticmethod
    def task_scopes(task: Task) -> list[str]:
        return ["platform:xiaohongshu", f"session:{task.session_ref}",
                f"device:{task.serial_hash}"]

    def capture_retry_status(self, task_id: str, retry_limit: int | None = None) -> dict:
        """Legacy protocol shape; historical credits are diagnostic, never a gate."""
        with self.sessions() as session:
            task = self._task(session, task_id)
            return {"exhausted": False, "available_attempts": None,
                    "granted_credits": self._read_credits(session, task_id),
                    "failure_count": task.retry_counts.get("capture", 0),
                    "pending_interrupted_attempt": task.pending_step in READ_STEPS,
                    "consecutive_failures": task.consecutive_read_failures,
                    "consecutive_no_progress": task.consecutive_no_progress,
                    "requires_inspection": max(task.consecutive_read_failures,
                                               task.consecutive_no_progress) >= 10,
                    "lifetime_limit_enabled": False}

    def read_retry_request_task(self, request_id: str) -> str | None:
        request_id = str(UUID(str(request_id)))
        with self.sessions() as session:
            row = session.get(TaskRetryGrant, request_id)
            return row.task_id if row else None

    @staticmethod
    def _settle_pending_read(task: Task) -> None:
        if not task.pending_step:
            return
        step = task.pending_step
        counts = dict(task.retry_counts)
        counts[step] = counts.get(step, 0) + 1
        task.retry_counts = counts
        if step in READ_STEPS:
            task.consecutive_read_failures += 1
        task.pending_step = None

    def reconcile_interrupted_read(self, task_id: str) -> None:
        """Caller owns the device lock; reconcile an abandoned owner once."""
        with self.sessions.begin() as session:
            task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status != "running":
                return
            self._settle_pending_read(task)
            for run in session.scalars(select(Run).where(
                Run.task_id == task_id, Run.status.in_(["running", "cooldown"]),
            )):
                run.status, run.ended_at = "interrupted", utcnow()
            task.status = "paused"
            task.stop_reason = ("consecutive_read_failures:10"
                                if task.consecutive_read_failures >= 10 else "owner_interrupted")
            task.updated_at = utcnow()
            session.add(Event(id=identifier(), task_id=task_id, run_id=None,
                              kind="read_retry_owner_reconciled", detail={
                                  "consecutive_failures": task.consecutive_read_failures,
                              }))

    def grant_read_retry(self, task_id: str, request_id: str, *, now=None) -> dict:
        """Compatibility endpoint. No new credits or authorization records are created.

        Existing requests remain bound to their historical task and old policy
        guards remain effective. A successful caller has checked the actual page.
        """
        request_id = str(UUID(str(request_id)))
        now = now or utcnow()
        with self.sessions() as session:
            task = self._task(session, task_id)
            existing = session.get(TaskRetryGrant, request_id)
            if existing and existing.task_id != task_id:
                raise ValueError("读取恢复请求已用于另一任务")
            if task.status != "paused":
                raise ValueError("只有已暂停的任务可以检查读取恢复")
            states = list(session.scalars(select(PolicyState).where(
                PolicyState.scope.in_(self.task_scopes(task)),
            )))
            if any(state.status == "manual" or state.probe_used or
                   (state.until is not None and aware(state.until) > now) for state in states):
                raise ValueError("存在冷却或人工处理限制，不能恢复读取")
            return {"granted": False, "already_granted": bool(existing), "credits": 0,
                    "checked": True, "lifetime_limit_enabled": False}

    def begin_step(self, task_id: str, step: str, retry_limit: int) -> bool:
        """Read streaks survive restart; action budgets remain cumulative."""
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            if step in READ_STEPS:
                if task.consecutive_read_failures >= READ_ANOMALY_THRESHOLD:
                    return False
            elif step.startswith("wait:"):
                if task.consecutive_no_progress >= READ_ANOMALY_THRESHOLD:
                    return False
            elif task.retry_counts.get(step, 0) > retry_limit:
                return False
            task.pending_step = step
            task.updated_at = utcnow()
            return True

    def complete_step(self, task_id: str, step: str) -> None:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            if task.pending_step == step:
                task.pending_step = None
            if step in READ_STEPS:
                task.consecutive_read_failures = 0
            task.updated_at = utcnow()

    def fail_step(self, task_id: str, step: str, retry_limit: int) -> bool:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            counts = dict(task.retry_counts)
            counts[step] = counts.get(step, 0) + 1
            task.retry_counts = counts
            if task.pending_step == step:
                task.pending_step = None
            task.updated_at = utcnow()
            if step in READ_STEPS:
                task.consecutive_read_failures += 1
                return task.consecutive_read_failures < READ_ANOMALY_THRESHOLD
            if step.startswith("wait:"):
                task.consecutive_no_progress += 1
                return task.consecutive_no_progress < READ_ANOMALY_THRESHOLD
            return counts[step] <= retry_limit

    def page_progress(self, task_id: str, *, advanced: bool) -> int:
        """Call after a requested page advancement, never for routine same-page reads."""
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            task.consecutive_no_progress = 0 if advanced else task.consecutive_no_progress + 1
            task.updated_at = utcnow()
            return task.consecutive_no_progress

    def update_state(self, task_id: str, run_id: str, status: str, reason: str | None = None):
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            task.status, task.stop_reason, task.updated_at = status, reason, utcnow()
            run = session.get(Run, run_id)
            if run:
                run.status = status

    def no_progress(self, task_id: str, made_progress: bool) -> int:
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            task.no_progress = 0 if made_progress else task.no_progress + 1
            return task.no_progress

    def seen(self, task_id: str, note_id: str | None) -> bool:
        if note_id is None:
            return False
        with self.sessions() as session:
            return (
                session.scalar(
                    select(Observation.id)
                    .where(
                        Observation.task_id == task_id,
                        Observation.note_id == note_id,
                        Observation.eligible.is_(True),
                    )
                    .limit(1)
                )
                is not None
            )

    def save_note(
        self,
        *,
        observation_id: str,
        task_id: str,
        run_id: str,
        note: ParsedNote,
        snapshot: Snapshot,
        evidence: list[dict],
    ) -> tuple[str, bool]:
        if not evidence:
            raise ValueError("evidence is required before committing an observation")
        fingerprint = hashlib.sha256(
            json.dumps(
                {k: v.raw for k, v in note.fields.items() if k in {"title", "body", "author"}},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            task = self._task(session, task_id)
            run = session.get(Run, run_id)
            if run is None or run.task_id != task_id:
                raise ValueError("run does not belong to this task")
            old = session.get(Observation, observation_id)
            if old is not None and old.task_id != task_id:
                raise ValueError("observation belongs to a different task")
            if old is None and note.note_id:
                old = session.scalar(
                    select(Observation).where(
                        Observation.task_id == task_id,
                        Observation.note_id == note.note_id,
                    )
                )
            if old is not None and (old.eligible or not note.eligible):
                return old.id, False
            row = old or Observation(
                id=observation_id,
                task_id=task_id,
                run_id=run_id,
                device_id=task.device_id,
                session_ref=task.session_ref,
            )
            previously_eligible = bool(old and old.eligible)
            row.run_id = run_id
            row.note_id, row.canonical_url = note.note_id, note.canonical_url
            row.identity_source, row.fingerprint = note.identity_source, fingerprint
            row.eligible, row.data = note.eligible, note.to_dict()
            row.schema_version = 2
            row.evidence = (old.evidence if old else []) + evidence
            row.app_version = snapshot.metadata.get("app_version")
            row.captured_at = snapshot.captured_at
            if old is None:
                session.add(row)
                task.observation_count += 1
            if note.eligible and not previously_eligible:
                task.eligible_count += 1
            # A new reading invalidates any earlier review of an incomplete observation.
            row.review_verdict = row.reviewer = row.review_identity = row.reviewed_at = None
            task.updated_at = utcnow()
            session.flush()
            return row.id, True

    def policy_states(self, scopes: list[str]) -> list[PolicyState]:
        with self.sessions() as session:
            return list(session.scalars(select(PolicyState).where(PolicyState.scope.in_(scopes))))

    def block(
        self,
        scopes: list[str],
        *,
        reason: str,
        until: datetime | None = None,
        probe_used: bool = False,
    ) -> None:
        with self.sessions.begin() as session:
            for scope in scopes:
                row = session.get(PolicyState, scope)
                if row is None:
                    row = PolicyState(scope=scope)
                    session.add(row)
                row.status = "cooldown" if until is not None else "manual"
                row.reason, row.until = reason, until
                row.probe_used, row.updated_at = probe_used, utcnow()

    def claim_probe(self, scopes: list[str]) -> None:
        with self.sessions.begin() as session:
            for scope in scopes:
                row = session.get(PolicyState, scope)
                if row:
                    row.probe_used = True

    def clear_blocks(self, scopes: list[str]) -> None:
        with self.sessions.begin() as session:
            for scope in scopes:
                row = session.get(PolicyState, scope)
                if row:
                    session.delete(row)

    def observations(self, task_id: str | None = None) -> list[dict]:
        with self.sessions() as session:
            query = select(Observation).order_by(Observation.captured_at, Observation.id)
            if task_id:
                query = query.where(Observation.task_id == task_id)
            return [object_dict(row) for row in session.scalars(query)]

    def status(self, task_id: str | None = None) -> dict:
        with self.sessions() as session:
            query = select(Task).order_by(Task.created_at)
            if task_id:
                self._task(session, task_id)
                query = query.where(Task.id == task_id)
            result = []
            for task in session.scalars(query):
                record = object_dict(task)
                base = select(Observation).where(Observation.task_id == task.id)
                rows = list(session.scalars(base))
                record["known_unique"] = len({r.note_id for r in rows if r.eligible and r.note_id})
                record["human_verified_unique"] = len(
                    {r.review_identity for r in rows if r.review_verdict == "accept"}
                )
                record["identity_unconfirmed"] = sum(
                    r.eligible and not r.note_id and r.review_verdict != "accept" for r in rows
                )
                fingerprints = {}
                for row in rows:
                    fingerprints.setdefault(row.fingerprint, []).append(row.id)
                record["possible_duplicates"] = [v for v in fingerprints.values() if len(v) > 1]
                result.append(record)
            policies = [object_dict(p) for p in session.scalars(select(PolicyState))]
        return {
            "tasks": result,
            "policy_states": policies,
            "real_device_acceptance": "not_inferred",
        }

    def review(
        self,
        observation_id: str,
        *,
        verdict: str,
        reviewer: str,
        identity: str | None,
    ) -> None:
        if verdict not in {"accept", "reject"} or not reviewer.strip():
            raise ValueError("review requires accept/reject and a nonempty reviewer")
        with self.sessions.begin() as session:
            row = session.get(Observation, observation_id)
            if row is None:
                raise ValueError("observation not found")
            if verdict == "accept":
                if not row.eligible:
                    raise ValueError("基础字段不可读的记录不能通过验收；请重新采集")
                if row.note_id:
                    key = f"note:{row.note_id}"
                    if identity and identity != key:
                        raise ValueError("可靠笔记 ID 不能被人工标识覆盖")
                elif identity and identity.strip():
                    key = identity if identity.startswith("note:") else f"manual:{identity.strip()}"
                else:
                    raise ValueError("无可靠笔记 ID，必须提供跨任务一致的人工 identity")
            else:
                key = None
            row.review_verdict, row.reviewer = verdict, reviewer.strip()
            row.review_identity, row.reviewed_at = key, utcnow()
            session.add(
                Event(
                    id=identifier(),
                    task_id=row.task_id,
                    run_id=row.run_id,
                    kind="human_review",
                    detail={
                        "observation_id": row.id,
                        "verdict": verdict,
                        "reviewer": reviewer,
                        "identity": key,
                    },
                )
            )

    def event_count(self) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(Event)) or 0
