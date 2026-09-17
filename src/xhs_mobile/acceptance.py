"""Evidence-checked data acceptance, independent of device runtime acceptance."""

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from xhs_mobile.domain import EvidenceError, base_readability, utcnow


def assign_unique_identities(
    identities_by_task: dict[str, list[str]], per_task: int = 10,
) -> dict[str, list[str]]:
    """Maximum bipartite matching from task slots to globally unique identities.

    Reassignment prevents a greedy first task from consuming identities needed by
    another task. Inputs are abstract identity keys; no device/data access occurs.
    """
    if per_task < 1:
        raise ValueError("per_task must be positive")
    choices = {task: sorted(set(values)) for task, values in identities_by_task.items()}
    owner: dict[str, tuple[str, int]] = {}
    assigned: dict[tuple[str, int], str] = {}

    def augment(slot: tuple[str, int], visited: set[str]) -> bool:
        for identity in choices[slot[0]]:
            if identity in visited:
                continue
            visited.add(identity)
            if identity not in owner or augment(owner[identity], visited):
                owner[identity] = slot
                assigned[slot] = identity
                return True
        return False

    for task in identities_by_task:
        for index in range(per_task):
            augment((task, index), set())
    return {
        task: [assigned[(task, i)] for i in range(per_task) if (task, i) in assigned]
        for task in identities_by_task
    }


def _moment(value: Any) -> datetime | None:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        return None


def _data_eligible(data: dict) -> bool:
    # Manual acceptance still requires a reviewed identity and intact evidence.
    # Automated body completeness is no longer claimed for either schema.
    return data.get("content_type") == "image_text" and base_readability(data)[0]


def _qualify(record: dict, task, evidence_store) -> list[str]:
    reasons = []
    if record.get("task_id") != task.id or record.get("device_id") != task.device_id:
        reasons.append("record_task_or_device_mismatch")
    if record.get("session_ref") != task.session_ref:
        reasons.append("record_session_mismatch")
    if record.get("schema_version") not in {1, 2} or record.get("platform") != "xiaohongshu":
        reasons.append("unsupported_record_schema_or_platform")
    data = record.get("data")
    if record.get("eligible") is not True or not isinstance(data, dict) or not _data_eligible(data):
        reasons.append("not_eligible_image_text_record")
    if record.get("review_verdict") != "accept":
        reasons.append("not_human_accepted")
    identity = record.get("review_identity")
    if not isinstance(identity, str) or not identity.strip():
        reasons.append("missing_review_identity")
    elif record.get("note_id") and identity != f"note:{record['note_id']}":
        reasons.append("review_identity_conflicts_with_note_id")
    reviewer = record.get("reviewer")
    if (not isinstance(reviewer, str) or not reviewer.strip()
            or _moment(record.get("reviewed_at")) is None):
        reasons.append("missing_review_audit")
    manifests = record.get("evidence")
    if not isinstance(manifests, list) or not manifests:
        return reasons + ["missing_evidence"]
    has_current_reading = False
    for manifest in manifests:
        if not isinstance(manifest, dict):
            reasons.append("invalid_evidence_manifest")
            continue
        metadata = manifest.get("metadata", {})
        if (not isinstance(metadata, dict) or metadata.get("source_kind") != "android"
                or bool(metadata.get("synthetic")) or bool(manifest.get("synthetic"))):
            reasons.append("non_android_or_synthetic_evidence")
            continue
        if manifest.get("device_id") != record.get("device_id"):
            reasons.append("evidence_device_mismatch")
        serial = metadata.get("serial")
        if (not isinstance(serial, str) or not serial
                or hashlib.sha256(serial.encode()).hexdigest() != task.serial_hash):
            reasons.append("evidence_device_serial_mismatch")
        try:
            if evidence_store.verify(manifest) is not True:
                reasons.append("evidence_integrity_failed")
                continue
        except (EvidenceError, OSError, ValueError, TypeError, KeyError):
            reasons.append("evidence_integrity_failed")
            continue
        if (
            manifest.get("run_id") == record.get("run_id")
            and _moment(manifest.get("captured_at")) == _moment(record.get("captured_at"))
            and _moment(record.get("captured_at")) is not None
            and metadata.get("app_version") == record.get("app_version")
            and bool(record.get("app_version"))
        ):
            has_current_reading = True
    if not has_current_reading:
        reasons.append("missing_evidence_for_current_reading")
    return sorted(set(reasons))


def acceptance_report(repository, task_ids: list[str], evidence_store) -> dict:
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "data_acceptance_only",
        "generated_at": utcnow().isoformat(),
        "passed": False,
        "required_tasks": 3,
        "required_per_task": 10,
        "required_global_unique": 30,
        "checks": {},
        "tasks": [],
        "selected_observation_ids": [],
        "issues": [],
        "runtime_recovery_acceptance": "requires_separate_laboratory_test_record",
    }
    if len(task_ids) != 3 or len(set(task_ids)) != 3:
        report["issues"].append("exactly_three_distinct_task_ids_required")
        return report
    tasks = []
    for task_id in task_ids:
        try:
            tasks.append(repository.task(task_id))
        except ValueError:
            report["issues"].append(f"task_not_found:{task_id}")
    if len(tasks) != 3:
        return report
    report["checks"].update({
        "three_search_tasks": all(task.mode == "search" for task in tasks),
        "three_distinct_keywords": len({task.keyword.strip() for task in tasks}) == 3
            and all(task.keyword.strip() for task in tasks),
        "single_device": len({(task.device_id, task.serial_hash) for task in tasks}) == 1,
    })
    for name, result in report["checks"].items():
        if not result:
            report["issues"].append(name + "_failed")
    choices: dict[str, dict[str, dict]] = {}
    for task in tasks:
        records = repository.observations(task.id)
        qualified: dict[str, dict] = {}
        rejected = []
        for record in records:
            reasons = _qualify(record, task, evidence_store)
            if reasons:
                rejected.append({"observation_id": record.get("id"), "reasons": reasons})
            else:
                qualified.setdefault(record["review_identity"].strip(), record)
        choices[task.id] = qualified
        report["tasks"].append({
            "task_id": task.id,
            "keyword": task.keyword,
            "observed_count": len(records),
            "accepted_count": sum(r.get("review_verdict") == "accept" for r in records),
            "qualified_unique_count": len(qualified),
            "selected_count": 0,
            "selected_observation_ids": [],
            "rejected_observations": rejected,
            "rejection_counts": dict(Counter(
                reason for rejected_record in rejected for reason in rejected_record["reasons"]
            )),
        })
    assignment = assign_unique_identities({key: list(value) for key, value in choices.items()})
    for task_report in report["tasks"]:
        task_id = task_report["task_id"]
        selected = [choices[task_id][identity]["id"] for identity in assignment[task_id]]
        task_report["selected_count"] = len(selected)
        task_report["selected_observation_ids"] = selected
        report["selected_observation_ids"].extend(selected)
        if len(selected) < 10:
            report["issues"].append(f"insufficient_disjoint_accepted_notes:{task_id}")
    report["available_global_unique"] = len({
        identity for group in choices.values() for identity in group
    })
    report["selected_global_unique"] = sum(map(len, assignment.values()))
    report["checks"]["ten_disjoint_notes_per_task"] = all(
        len(assignment[task.id]) == 10 for task in tasks
    )
    report["passed"] = all(report["checks"].values()) and not report["issues"]
    return report
