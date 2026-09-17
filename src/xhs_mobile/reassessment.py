"""Audited local reassessment. Never opens a phone or starts/resumes any task.

Call only after taking the deployment database backup. ``dry_run=True`` is the
default and performs no writes. Applying changes is a single transaction with
the previous assessments and counters retained in an immutable audit table.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from xhs_mobile.domain import EvidenceError, PageError, Snapshot, base_readability
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.models import Observation, QualityAssessmentAudit, Run, Task
from xhs_mobile.parser import page_time_kind, parse_note
from xhs_mobile.profile import Profile
from xhs_mobile.ui_readback import xml_sanitized

QUALITY_REVISION = 2
_OBSOLETE_WARNINGS = {"body_not_fully_visible", "body_not_confirmed_complete"}


def _same_observed_text(previous: dict, current) -> bool:
    """Compare representations within an already associated detail visit only.

    u2.jar replaces UTF-16 surrogate units with dots. A separately verified
    objInfo value can therefore differ from the original XML without a page
    change. Reproduce the lossy serialization only for this consistency check;
    never restore unknown text, assign an identity, or edit either original.
    """
    old, new = previous.get("raw"), current.raw
    if old == new:
        return True
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    return (
        previous.get("method") == "ui_objinfo" and current.method == "ui"
        and xml_sanitized(old) == new
    ) or (
        previous.get("method") == "ui" and current.method == "ui_objinfo"
        and old == xml_sanitized(new)
    )


def _existing_time(row: Observation, profile: Profile, store: EvidenceStore) -> tuple[dict, list]:
    """Only reuse verified evidence belonging to this observation's detail visit."""
    candidates = []
    errors = []
    fields = row.data.get("fields", {})
    for manifest in row.evidence:
        if manifest.get("run_id") != row.run_id:
            continue
        try:
            store.verify(manifest)
            snapshot = Snapshot(
                # Preserve CRLF exactly: objInfo provenance hashes the original
                # UTF-8 hierarchy, so universal-newline conversion invalidates it.
                xml=store._safe_path(manifest["files"]["xml"]["path"]).read_bytes().decode("utf-8"),
                png=store._safe_path(manifest["files"]["png"]["path"]).read_bytes(),
                captured_at=datetime.fromisoformat(manifest["captured_at"]),
                metadata=manifest["metadata"],
            )
            note = parse_note(snapshot, profile)
            if note.content_type != "image_text":
                continue
            # The manifest was already associated with this visit when saved.
            # Reject any visible author/title contradiction before supplementing
            # fields; no text-derived note identity is created here.
            if any(
                note.fields[name].status == "present" and fields.get(name, {}).get("raw")
                and not _same_observed_text(fields[name], note.fields[name])
                for name in ("title", "author")
            ):
                errors.append("time_evidence_conflicts_with_observation")
                continue
            value = note.fields["published_at"]
            if value.status == "present" and value.raw:
                value.evidence_ref = manifest["manifest_path"]
                candidates.append(asdict(value))
        except (EvidenceError, PageError, OSError, ValueError, KeyError) as exc:
            errors.append(f"time_evidence_unusable:{type(exc).__name__}")
    if len({item["raw"] for item in candidates}) == 1:
        return candidates[0], errors
    if candidates:
        errors.append("time_evidence_ambiguous")
    return {}, errors


def _reassess(row: Observation, profile: Profile, store: EvidenceStore) -> tuple[dict, list]:
    data = deepcopy(row.data)
    fields = data.setdefault("fields", {})
    for name in ("title", "body", "author"):
        field = fields.get(name, {})
        raw = field.get("raw")
        rule = profile.fields.get(name)
        still_rejected = bool(rule and raw and (
            any(raw.endswith(value) for value in rule.reject_text_suffixes
                if value.strip(".…。． \t\r\n"))
            or any(value in raw for value in rule.reject_text_contains
                   if value.strip(".…。． \t\r\n"))
        ))
        if (raw and field.get("reason") == "possible_ui_truncation_or_placeholder"
                and "�" not in raw and not still_rejected):
            # Old punctuation-based rejection is reversible without editing raw.
            field["status"] = "present"
            field["reason"] = None
    time, errors = _existing_time(row, profile, store)
    old_time = fields.get("published_at", {})
    if time and (old_time.get("status") != "present" or old_time.get("raw") == time["raw"]):
        fields["published_at"] = time
    elif time and old_time.get("raw") != time["raw"]:
        errors.append("existing_time_conflicts_with_evidence")
    data.update({
        "quality_revision": QUALITY_REVISION,
        "body_complete": None,
        "completeness_status": "not_assessed",
        "identity_status": "verified" if row.note_id else "unverified",
        "time_kind": page_time_kind(fields.get("published_at", {})),
        "warnings": [w for w in data.get("warnings", []) if w not in _OBSOLETE_WARNINGS],
    })
    # Prior uncalibrated hashtags are never promoted into confirmed topics.
    # Preserve any raw field data and its original state in the audit, not as a
    # newly asserted platform topic. Confirmed data from revision 2 is skipped.
    data["topics_status"] = "unrecognized"
    data["topics"] = []
    data["topic_sources"] = []
    tags = fields.setdefault("tags", {})
    tags.update({"status": "not_readable", "reason": "platform_topics_unrecognized"})
    return data, sorted(set(errors))


def reassess_history(
    engine: Engine, *, profile: Profile, evidence_root: str | Path, dry_run: bool = True,
) -> dict:
    """Recompute quality and optional original times, preserving every raw base field.

    Applying is idempotent by per-record revision and audit unique key. Existing
    task statuses, targets, pause requests, consumed budgets and review decisions
    remain unchanged, even if the new readable count already reaches its target.
    """
    summary = {"dry_run": dry_run, "revision": QUALITY_REVISION, "observations": 0,
               "changed": 0, "eligible_before": 0, "eligible_after": 0,
               "time_before": 0, "time_after": 0, "tasks_changed": 0, "issues": []}
    store = EvidenceStore(evidence_root)
    with Session(engine) as session, session.begin():
        if not dry_run and (
            session.scalar(select(Task.id).where(Task.status == "running").limit(1))
            or session.scalar(select(Run.id).where(Run.status == "running").limit(1))
        ):
            raise ValueError("Stop collection before applying historical reassessment")
        task_query, row_query = select(Task).order_by(Task.id), select(Observation)
        if not dry_run:
            task_query = task_query.with_for_update()
            row_query = row_query.with_for_update()
        tasks = list(session.scalars(task_query))
        rows = list(session.scalars(row_query.order_by(Observation.id)))
        counts = {task.id: 0 for task in tasks}
        changed_tasks = set()
        for row in rows:
            summary["observations"] += 1
            summary["eligible_before"] += int(row.eligible)
            summary["time_before"] += int(
                row.data.get("fields", {}).get("published_at", {}).get("status") == "present"
            )
            if row.data.get("quality_revision", 1) >= QUALITY_REVISION:
                data, errors = row.data, []
            else:
                data, errors = _reassess(row, profile, store)
                summary["changed"] += 1
                changed_tasks.add(row.task_id)
                if not dry_run:
                    session.add(QualityAssessmentAudit(
                        id=str(uuid4()), revision=QUALITY_REVISION,
                        entity_type="observation", entity_id=row.id,
                        before={"data": deepcopy(row.data), "eligible": row.eligible,
                                "schema_version": row.schema_version},
                        after={"data": data, "eligible": base_readability(data)[0],
                               "schema_version": 2, "issues": errors},
                    ))
                    row.data = data
                    row.eligible = base_readability(data)[0]
                    row.schema_version = 2
            readable = base_readability(data)[0]
            counts[row.task_id] += int(readable)
            summary["eligible_after"] += int(readable)
            summary["time_after"] += int(
                data.get("fields", {}).get("published_at", {}).get("status") == "present"
            )
            if errors:
                summary["issues"].append({"observation_id": row.id, "reasons": errors})
        for task in tasks:
            if task.id not in changed_tasks:
                continue
            summary["tasks_changed"] += int(task.eligible_count != counts[task.id])
            if not dry_run:
                session.add(QualityAssessmentAudit(
                    id=str(uuid4()), revision=QUALITY_REVISION, entity_type="task",
                    entity_id=task.id,
                    before={"eligible_count": task.eligible_count,
                            "observation_count": task.observation_count,
                            "target": task.target, "status": task.status,
                            "policy": task.policy, "detail_visits": task.detail_visits,
                            "list_swipes": task.list_swipes, "retry_counts": task.retry_counts},
                    after={"eligible_count": counts[task.id]},
                ))
                task.eligible_count = counts[task.id]
    return summary
