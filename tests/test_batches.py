"""Offline batch tests; every task and observation is explicitly SYNTHETIC."""

import pytest
from sqlalchemy import create_engine, event, func, select

from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import Policy, Settings
from xhs_mobile.domain import FieldValue, ParsedNote, Snapshot
from xhs_mobile.models import Base, Batch, BatchItem, Task
from xhs_mobile.repository import Repository, identifier, serial_hash


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic-batches.db'}")
    Base.metadata.create_all(engine)
    yield Repository(engine)
    engine.dispose()


def create(batches, **overrides):
    args = {
        "device_id": "SYNTHETIC-device",
        "serial": "SYNTHETIC-serial",
        "session_ref": "SYNTHETIC-session",
        "keywords": [" SYNTHETIC 甲 ", "SYNTHETIC 乙", "SYNTHETIC 丙"],
        "target": 10,
        "policy": Policy().model_dump(),
    }
    return batches.create_batch(**(args | overrides))


def table_counts(repo):
    with repo.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Batch, Task, BatchItem)
        )


def test_order_and_binding_survive_new_repository_without_recreating_tasks(repo):
    batches = BatchRepository(repo)
    batch_id = create(batches)
    task_ids = batches.task_ids(batch_id)
    recovered = BatchRepository(Repository(repo.engine))
    assert recovered.task_ids(batch_id) == task_ids
    assert table_counts(repo) == (1, 3, 3)
    assert len({batch_id, *task_ids}) == 4
    batch = recovered.batch(batch_id)
    assert batch.serial_hash == serial_hash("SYNTHETIC-serial")
    report = recovered.status(batch_id)
    assert report["real_device_acceptance"] == "not_inferred"
    tasks = report["batches"][0]["tasks"]
    assert [task["position"] for task in tasks] == [0, 1, 2]
    assert [task["keyword"] for task in tasks] == [
        "SYNTHETIC 甲", "SYNTHETIC 乙", "SYNTHETIC 丙"
    ]
    for task in tasks:
        assert task["device_id"] == batch.device_id
        assert task["serial_hash"] == batch.serial_hash
        assert task["session_ref"] == batch.session_ref
        assert task["mode"] == "search"


@pytest.mark.parametrize(
    "overrides",
    [
        {"keywords": []},
        {"keywords": [" "]},
        {"keywords": [None]},
        {"keywords": "SYNTHETIC not a list"},
        {"keywords": ["SYNTHETIC 甲", " SYNTHETIC 甲 "]},
        {"keywords": [f"SYNTHETIC {i}" for i in range(21)]},
        {"target": 0},
        {"target": 501},
        {"target": True},
        {"target": 1.5},
        {"target": 10, "policy": {"max_detail_visits": 9}},
        {"policy": {"max_detail_visits": 0}},
        {"device_id": " "},
        {"device_id": "x" * 129},
        {"session_ref": ""},
        {"serial": " "},
    ],
)
def test_invalid_batch_leaves_no_partial_tasks(repo, overrides):
    with pytest.raises(ValueError):
        create(BatchRepository(repo), **overrides)
    assert table_counts(repo) == (0, 0, 0)


@pytest.mark.parametrize("target", [100, 101, 500])
def test_large_targets_persist_one_generated_policy_per_child(repo, target):
    policy = Settings().new_task_policy(target).model_dump()
    batches = BatchRepository(repo)
    batch_id = create(batches, target=target, policy=policy)
    reopened = BatchRepository(Repository(repo.engine))
    for task_id in reopened.task_ids(batch_id):
        task = reopened.repository.task(task_id)
        assert task.target == target
        assert task.policy == policy
    assert table_counts(repo) == (1, 3, 3)


@pytest.mark.parametrize("failure_point", ["after_flush_postexec", "before_commit"])
def test_process_failure_after_flush_or_before_commit_rolls_back_everything(repo, failure_point):
    batches = BatchRepository(repo)

    def fail(*args):
        raise RuntimeError("SYNTHETIC interrupted transaction")

    event.listen(repo.sessions, failure_point, fail)
    try:
        with pytest.raises(RuntimeError, match="SYNTHETIC"):
            create(batches)
    finally:
        event.remove(repo.sessions, failure_point, fail)
    assert table_counts(repo) == (0, 0, 0)
    create(batches)
    assert table_counts(repo) == (1, 3, 3)


def test_pause_and_resume_metadata_preserve_child_counters_and_policy(repo):
    batches = BatchRepository(repo)
    batch_id = create(batches)
    first, second, third = batches.task_ids(batch_id)
    run = repo.start_run(second, "SYNTHETIC-profile")
    repo.consume(second, "detail_visits", 100)
    repo.consume_retry(second, "SYNTHETIC-step", 2)
    repo.update_state(second, run, "cooldown", "rate_limited")
    batches.update(batch_id, "running")
    batches.request_pause(batch_id)
    assert batches.batch(batch_id).pause_requested
    assert repo.task(second).pause_requested
    assert not repo.task(first).pause_requested
    assert not repo.task(third).pause_requested
    batches.update(batch_id, "paused", "operator_requested")
    batches.update(batch_id, "running")
    assert not batches.batch(batch_id).pause_requested
    child = repo.task(second)
    assert child.detail_visits == 1
    assert child.retry_counts == {"SYNTHETIC-step": 1}
    assert child.status == "cooldown" and child.pause_requested
    assert child.stop_reason == "rate_limited"
    assert table_counts(repo) == (1, 3, 3)


def test_status_keeps_missing_identity_and_review_counts_separate(repo):
    batches = BatchRepository(repo)
    batch_id = create(batches, keywords=["SYNTHETIC 甲"])
    task_id = batches.task_ids(batch_id)[0]
    run_id = repo.start_run(task_id, "SYNTHETIC-profile")
    parsed = ParsedNote(
        fields={
            name: FieldValue(raw=f"SYNTHETIC {name}", status="present", method="ui")
            for name in ("title", "body", "author")
        },
        body_complete=True,
    )
    for _ in range(2):
        repo.save_note(
            observation_id=identifier(), task_id=task_id, run_id=run_id, note=parsed,
            snapshot=Snapshot(xml="<synthetic/>", png=b"SYNTHETIC"),
            evidence=[{"source_kind": "synthetic"}],
        )
    batches.update(batch_id, "collected_awaiting_review")
    task = batches.status(batch_id)["batches"][0]["tasks"][0]
    assert task["observation_count"] == task["eligible_count"] == 2
    assert task["identity_unconfirmed"] == 2
    assert task["known_unique"] == task["human_verified_unique"] == 0
    assert len(task["possible_duplicates"][0]) == 2


def test_missing_batch_and_unknown_status_are_explicit(repo):
    batches = BatchRepository(repo)
    for operation in (batches.batch, batches.status, batches.task_ids, batches.request_pause):
        with pytest.raises(ValueError, match="没有批次"):
            operation("SYNTHETIC-missing")
    batch_id = create(batches)
    with pytest.raises(ValueError, match="未知批次状态"):
        batches.update(batch_id, "accepted")
    assert batches.batch(batch_id).status == "pending"
