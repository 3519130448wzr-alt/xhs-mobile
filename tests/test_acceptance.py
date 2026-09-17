"""All records are SYNTHETIC / TEST_ONLY; these tests prove no live acceptance."""

import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from xhs_mobile.acceptance import acceptance_report, assign_unique_identities
from xhs_mobile.domain import EvidenceError

STAMP = "2026-09-12T00:00:00+00:00"


class TestOnlyRepository:
    __test__ = False

    def __init__(self, records):
        self.rows = records
        self.tasks = {
            name: SimpleNamespace(id=name, keyword=f"SYNTHETIC-{name}",
                                  mode="search", device_id="SYNTHETIC-device",
                                  session_ref="SYNTHETIC-session",
                                  serial_hash=hashlib.sha256(b"SYNTHETIC-serial").hexdigest())
            for name in ("task-a", "task-b", "task-c")
        }

    def task(self, task_id):
        if task_id not in self.tasks:
            raise ValueError("TEST_ONLY missing task")
        return self.tasks[task_id]

    def observations(self, task_id):
        return self.rows[task_id]


class TestOnlyEvidenceVerifier:
    """In-memory TEST_ONLY double. It makes no filesystem or authenticity claim."""

    __test__ = False

    def verify(self, manifest):
        if manifest.get("TEST_ONLY_integrity_failure"):
            raise EvidenceError("TEST_ONLY failure")
        return True


def test_matching_reassigns_common_identity_to_constrained_task():
    choices = {"task-a": ["shared", "unique-a"], "task-b": ["shared"]}
    result = assign_unique_identities(choices, per_task=1)
    assert result == {"task-a": ["unique-a"], "task-b": ["shared"]}


def test_matching_three_tasks_thirty_slots_with_overlap():
    # Pure abstract keys, not claimed observations or device data.
    choices = {
        "a": [f"key-{i:02}" for i in range(20)],
        "b": [f"key-{i:02}" for i in range(10)],
        "c": [f"key-{i:02}" for i in range(10, 30)],
    }
    selected = assign_unique_identities(choices)
    assert all(len(values) == 10 for values in selected.values())
    assert len({value for values in selected.values() for value in values}) == 30


def test_matching_global_thirty_is_insufficient_when_two_tasks_share_only_ten():
    choices = {"a": [f"shared-{i}" for i in range(10)],
               "b": [f"shared-{i}" for i in range(10)],
               "c": [f"other-{i}" for i in range(20)]}
    assert len({value for group in choices.values() for value in group}) == 30
    selected = assign_unique_identities(choices)
    assert sum(map(len, selected.values())) == 20


def test_matching_duplicate_edges_do_not_create_extra_notes():
    assert assign_unique_identities({"a": ["x", "x"]}, per_task=2) == {"a": ["x"]}


def make_record(task, index, *, source="synthetic"):
    """TEST_ONLY synthetic dictionary; source='android' is for verifier branch tests only."""
    return {
        "id": f"SYNTHETIC-{task}-{index}", "task_id": task, "device_id": "SYNTHETIC-device",
        "session_ref": "SYNTHETIC-session",
        "run_id": f"SYNTHETIC-run-{task}", "platform": "xiaohongshu", "schema_version": 1,
        "note_id": None, "eligible": True, "app_version": "SYNTHETIC-1",
        "captured_at": STAMP, "review_verdict": "accept", "reviewer": "TEST_ONLY reviewer",
        "review_identity": f"manual:SYNTHETIC-{task}-{index}", "reviewed_at": STAMP,
        "data": {
            "content_type": "image_text", "body_complete": True,
            "fields": {name: {"raw": f"SYNTHETIC {name}", "status": "present", "method": "ui"}
                       for name in ("title", "body", "author")},
        },
        "evidence": [{
            "device_id": "SYNTHETIC-device", "run_id": f"SYNTHETIC-run-{task}",
            "captured_at": STAMP, "metadata": {
                "source_kind": source, "app_version": "SYNTHETIC-1", "serial": "SYNTHETIC-serial",
            },
            "TEST_ONLY": "In-memory unit test; never a persisted real evidence manifest",
        }],
    }


def repository(*, source="synthetic"):
    return TestOnlyRepository({name: [make_record(name, i, source=source) for i in range(10)]
                               for name in ("task-a", "task-b", "task-c")})


def report(repo):
    return acceptance_report(repo, list(repo.tasks), TestOnlyEvidenceVerifier())


def test_synthetic_evidence_cannot_count_even_with_accepted_review():
    result = report(repository())
    assert not result["passed"]
    assert result["selected_global_unique"] == 0
    assert all(task["rejection_counts"]["non_android_or_synthetic_evidence"] == 10
               for task in result["tasks"])


def test_test_only_verifier_branch_and_selection_report():
    # Deliberately exercises the Android-source branch with an in-memory verifier.
    # This is software testing, not a real-device acceptance result.
    result = report(repository(source="android"))
    assert result["passed"]
    assert len(result["selected_observation_ids"]) == 30
    assert result["scope"] == "data_acceptance_only"
    assert result["runtime_recovery_acceptance"] == "requires_separate_laboratory_test_record"


@pytest.mark.parametrize("mutation,reason", [
    (lambda row: row.update(review_verdict="reject"), "not_human_accepted"),
    (lambda row: row.update(review_identity=None), "missing_review_identity"),
    (lambda row: row.update(eligible=False), "not_eligible_image_text_record"),
    (lambda row: row["data"]["fields"]["body"].update(raw=None),
     "not_eligible_image_text_record"),
    (lambda row: row.update(schema_version=999), "unsupported_record_schema_or_platform"),
    (lambda row: row.update(evidence=[]), "missing_evidence"),
    (lambda row: row["evidence"][0].update(TEST_ONLY_integrity_failure=True),
     "evidence_integrity_failed"),
    (lambda row: row["evidence"][0]["metadata"].update(synthetic=True),
     "non_android_or_synthetic_evidence"),
    (lambda row: row["evidence"][0].update(run_id="other-run"),
     "missing_evidence_for_current_reading"),
    (lambda row: row["evidence"][0].update(device_id="other-device"), "evidence_device_mismatch"),
    (lambda row: row.update(note_id="reliable-id"), "review_identity_conflicts_with_note_id"),
    (lambda row: row.update(session_ref="SYNTHETIC-other-session"), "record_session_mismatch"),
    (lambda row: row["evidence"][0]["metadata"].update(serial="SYNTHETIC-other-serial"),
     "evidence_device_serial_mismatch"),
])
def test_bad_record_prevents_acceptance(mutation, reason):
    repo = repository(source="android")
    mutation(repo.rows["task-a"][0])
    result = report(repo)
    assert not result["passed"]
    assert result["tasks"][0]["rejection_counts"][reason] == 1


def test_historical_evidence_permitted_only_with_current_verified_reading():
    repo = repository(source="android")
    old = deepcopy(repo.rows["task-a"][0]["evidence"][0])
    old["run_id"] = "SYNTHETIC-old-run"
    repo.rows["task-a"][0]["evidence"].insert(0, old)
    assert report(repo)["passed"]


@pytest.mark.parametrize("task_ids", [[], ["task-a"] * 3, ["task-a", "task-b"],
                                    ["task-a", "task-b", "missing"]])
def test_exactly_three_real_task_references_required(task_ids):
    assert not acceptance_report(repository(), task_ids, TestOnlyEvidenceVerifier())["passed"]


def test_search_keywords_and_single_device_are_required():
    repo = repository(source="android")
    repo.tasks["task-b"].keyword = repo.tasks["task-a"].keyword
    assert not report(repo)["passed"]
    repo = repository(source="android")
    repo.tasks["task-b"].mode = "current"
    assert not report(repo)["passed"]
    repo = repository(source="android")
    repo.tasks["task-b"].device_id = "SYNTHETIC-other-device"
    assert not report(repo)["passed"]
    repo = repository(source="android")
    repo.tasks["task-b"].serial_hash = hashlib.sha256(b"SYNTHETIC-other-serial").hexdigest()
    assert not report(repo)["passed"]


def test_real_repository_interface_keeps_synthetic_rows_out_of_acceptance(tmp_path):
    """SQLite repository integration with SYNTHETIC data, never a device acceptance."""
    from sqlalchemy import create_engine

    from xhs_mobile.domain import FieldValue, ParsedNote, Snapshot
    from xhs_mobile.exports import export_records
    from xhs_mobile.models import Base
    from xhs_mobile.repository import Repository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    task_ids = []
    for index in range(3):
        task = repo.create_task(
            device_id="SYNTHETIC-device", serial="SYNTHETIC-serial",
            session_ref="SYNTHETIC-session", keyword=f"SYNTHETIC-{index}", target=10, policy={},
        )
        task_ids.append(task.id)
        run_id = repo.start_run(task.id, "SYNTHETIC-profile-hash")
        snapshot = Snapshot(xml="<SYNTHETIC/>", png=b"SYNTHETIC-not-PNG", metadata={
            "source_kind": "synthetic", "serial": "SYNTHETIC-serial", "app_version": "SYNTHETIC-1",
        })
        note = ParsedNote(fields={
            name: FieldValue(raw=f"SYNTHETIC {name}", status="present", method="ui")
            for name in ("title", "body", "author")
        }, body_complete=True)
        observation_id, inserted = repo.save_note(
            observation_id=f"SYNTHETIC-observation-{index}", task_id=task.id, run_id=run_id,
            note=note, snapshot=snapshot, evidence=[{
                "metadata": snapshot.metadata, "device_id": task.device_id, "run_id": run_id,
                "captured_at": snapshot.captured_at.isoformat(),
            }],
        )
        assert inserted
        repo.review(observation_id, verdict="accept", reviewer="TEST_ONLY review",
                    identity=f"SYNTHETIC-identity-{index}")
    result = acceptance_report(repo, task_ids, TestOnlyEvidenceVerifier())
    assert not result["passed"]
    assert result["selected_global_unique"] == 0
    assert all(task["accepted_count"] == 1 for task in result["tasks"])
    output = export_records(repo.observations(), tmp_path / "synthetic-rows.jsonl", "jsonl")
    assert output["count"] == 3
    engine.dispose()


@pytest.mark.parametrize("legacy_complete", [True, False, None])
@pytest.mark.parametrize("schema_version", [1, 2])
def test_human_acceptance_does_not_require_automatic_completeness(schema_version, legacy_complete):
    repo = repository(source="android")
    for rows in repo.rows.values():
        for row in rows:
            row["schema_version"] = schema_version
            row["data"].update(body_complete=legacy_complete, completeness_status="not_assessed")
    assert report(repo)["passed"]
    repo.rows["task-a"][0]["review_verdict"] = None
    assert not report(repo)["passed"]
