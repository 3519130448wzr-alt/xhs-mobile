"""SYNTHETIC batch orchestration tests using isolated on-disk SQLite databases.

Child execution is a bounded callback; no phone, real selector, live database,
or collected user data is accessed. Real task and batch repositories exercise
durable ordering, checkpoint retention, and cancellation between keywords.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from xhs_mobile.batch_runner import BatchRunner
from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import Policy
from xhs_mobile.models import Base, Task
from xhs_mobile.repository import Repository


@pytest.fixture
def batch(tmp_path):
    url = f"sqlite:///{tmp_path / 'synthetic-batches.sqlite'}"
    repo = Repository(create_engine(url))
    Base.metadata.create_all(repo.engine)
    batches = BatchRepository(repo)
    batch_id = batches.create_batch(
        device_id="SYNTHETIC-device", serial="SYNTHETIC-serial",
        session_ref="SYNTHETIC-session",
        keywords=["SYNTHETIC 甲", "SYNTHETIC 乙", "SYNTHETIC 丙"],
        target=10, policy=Policy().model_dump(),
    )
    value = SimpleNamespace(
        repo=repo, batches=batches, id=batch_id, ids=batches.task_ids(batch_id), url=url,
    )
    yield value
    value.repo.engine.dispose()


def finish(repo, task_id, status="collected_awaiting_review", reason="target_collected"):
    run_id = repo.start_run(task_id, "SYNTHETIC-profile-hash")
    repo.finish(task_id, run_id, status, reason)


def orchestrator(batch, callback, **kwargs):
    return BatchRunner(
        repository=batch.repo, batches=batch.batches, execute_task=callback, **kwargs,
    )


def test_ordered_children_finish_before_next_keyword_and_do_not_imply_acceptance(batch):
    calls = []

    def execute(task_id, stopped, acknowledge):
        assert not stopped()
        assert acknowledge is False
        assert batch.batches.batch(batch.id).status == "running"
        calls.append(("execute", task_id))
        finish(batch.repo, task_id)

    result = orchestrator(
        batch, execute, task_finished=lambda task_id: calls.append(("export", task_id)),
    ).execute(batch.id)

    assert calls == [item for task_id in batch.ids for item in [
        ("execute", task_id), ("export", task_id),
    ]]
    assert result["batches"][0]["status"] == "collected_awaiting_review"
    assert result["batches"][0]["stop_reason"] == "all_targets_collected"
    assert [task["id"] for task in result["batches"][0]["tasks"]] == batch.ids
    assert all(task["human_verified_unique"] == 0 for task in result["batches"][0]["tasks"])
    assert result["real_device_acceptance"] == "not_inferred"


def test_durable_batch_pause_reaches_active_child_and_prevents_next_keyword(batch):
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append(task_id)
        run_id = batch.repo.start_run(task_id, "SYNTHETIC-profile-hash")
        batch.batches.request_pause(batch.id)
        assert stopped()
        assert batch.repo.task(task_id).pause_requested
        batch.repo.finish(task_id, run_id, "paused", "operator_pause")

    orchestrator(batch, execute).execute(batch.id)

    assert calls == batch.ids[:1]
    persisted = batch.batches.batch(batch.id)
    assert persisted.status == "paused"
    assert persisted.stop_reason == "operator_pause"
    assert persisted.pause_requested
    assert [batch.repo.task(task_id).status for task_id in batch.ids[1:]] == ["pending"] * 2


def test_fresh_process_resume_reuses_original_ids_and_committed_task_budgets(batch):
    finish(batch.repo, batch.ids[0])
    with batch.repo.sessions.begin() as session:
        task = session.get(Task, batch.ids[1])
        task.status, task.stop_reason = "paused", "device_unavailable"
        task.detail_visits, task.list_swipes = 7, 4
        task.retry_counts = {"open_detail": 2, "body_swipes": 9}
        task.no_progress = 2
    batch.batches.request_pause(batch.id)
    batch.batches.update(batch.id, "paused", "operator_pause")
    original_count = len(batch.repo.status()["tasks"])
    batch.repo.engine.dispose()
    batch.repo = Repository(create_engine(batch.url))
    batch.batches = BatchRepository(batch.repo)
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append(task_id)
        assert not stopped()
        if task_id == batch.ids[1]:
            task = batch.repo.task(task_id)
            assert (task.detail_visits, task.list_swipes, task.no_progress) == (7, 4, 2)
            assert task.retry_counts == {"open_detail": 2, "body_swipes": 9}
            assert task.target == 10
        finish(batch.repo, task_id)

    orchestrator(batch, execute).execute(batch.id)

    assert calls == batch.ids[1:]
    assert batch.batches.task_ids(batch.id) == batch.ids
    assert len(batch.repo.status()["tasks"]) == original_count
    assert batch.repo.task(batch.ids[1]).detail_visits == 7
    assert batch.repo.task(batch.ids[1]).retry_counts == {"open_detail": 2, "body_swipes": 9}
    assert not batch.batches.batch(batch.id).pause_requested


@pytest.mark.parametrize("reason", [
    "no_new_candidates", "detail_budget_exhausted", "swipe_budget_exhausted",
])
def test_natural_attempt_end_allows_next_keyword_and_keeps_batch_partial(batch, reason):
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append(task_id)
        if task_id == batch.ids[0]:
            finish(batch.repo, task_id, "partial", reason)
        else:
            finish(batch.repo, task_id)

    orchestrator(batch, execute).execute(batch.id)

    assert calls == batch.ids
    assert batch.batches.batch(batch.id).status == "partial"
    assert batch.batches.batch(batch.id).stop_reason == "one_or_more_targets_not_reached"
    assert batch.repo.task(batch.ids[0]).stop_reason == reason


@pytest.mark.parametrize(("status", "reason"), [
    ("paused", "login_required"),
    ("paused", "unknown_page"),
    ("cooldown", "rate_limited"),
    ("partial", "current_detail_incomplete"),
    ("failed", "evidence_save_failed"),
    ("paused", None),
])
def test_unresolved_child_stops_entire_batch_before_next_keyword(batch, status, reason):
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append(task_id)
        finish(batch.repo, task_id, status, reason)

    orchestrator(batch, execute).execute(batch.id)

    assert calls == batch.ids[:1]
    assert batch.batches.batch(batch.id).status == "paused"
    assert batch.batches.batch(batch.id).stop_reason == (reason or status)
    assert all(batch.repo.task(task_id).status == "pending" for task_id in batch.ids[1:])


def test_cancellation_after_saved_child_is_durable_and_resume_skips_that_child(batch):
    stopped = False
    calls = []

    def execute(task_id, should_stop, acknowledge):
        calls.append(task_id)
        finish(batch.repo, task_id)

    def exported(task_id):
        nonlocal stopped
        stopped = True

    orchestrator(
        batch, execute, task_finished=exported, stop_requested=lambda: stopped,
    ).execute(batch.id)

    assert calls == batch.ids[:1]
    assert batch.repo.task(batch.ids[0]).status == "collected_awaiting_review"
    assert batch.batches.batch(batch.id).status == "paused"
    assert batch.batches.batch(batch.id).stop_reason == "operator_pause"
    stopped = False
    orchestrator(batch, execute, stop_requested=lambda: stopped).execute(batch.id)
    assert calls == batch.ids


def test_cancellation_before_first_keyword_operates_no_child(batch):
    orchestrator(
        batch, lambda *_: pytest.fail("a child was started after cancellation"),
        stop_requested=lambda: True,
    ).execute(batch.id)
    assert batch.batches.batch(batch.id).status == "paused"
    assert all(batch.repo.task(task_id).status == "pending" for task_id in batch.ids)


def test_acknowledgement_applies_only_to_first_executed_child_after_completed_items(batch):
    finish(batch.repo, batch.ids[0])
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append((task_id, acknowledge))
        finish(batch.repo, task_id)

    orchestrator(batch, execute).execute(batch.id, acknowledge=True)
    assert calls == [(batch.ids[1], True), (batch.ids[2], False)]


def test_export_callback_failure_retains_saved_child_and_blocks_next_keyword(batch):
    calls = []

    def execute(task_id, stopped, acknowledge):
        calls.append(task_id)
        finish(batch.repo, task_id)

    def export_failure(task_id):
        assert batch.repo.task(task_id).status == "collected_awaiting_review"
        raise OSError("SYNTHETIC full export volume")

    with pytest.raises(OSError, match="SYNTHETIC full export volume"):
        orchestrator(batch, execute, task_finished=export_failure).execute(batch.id)

    assert calls == batch.ids[:1]
    assert batch.repo.task(batch.ids[0]).status == "collected_awaiting_review"
    assert all(batch.repo.task(task_id).status == "pending" for task_id in batch.ids[1:])
    assert batch.batches.batch(batch.id).status == "paused"
    assert batch.batches.batch(batch.id).stop_reason == "execution_error:OSError"


def test_child_exception_preserves_checkpoint_and_does_not_export_or_start_next(batch):
    def execute(task_id, stopped, acknowledge):
        assert batch.repo.consume(task_id, "detail_visits", 100)
        raise ConnectionError("SYNTHETIC disconnection")

    with pytest.raises(ConnectionError, match="SYNTHETIC disconnection"):
        orchestrator(
            batch, execute, task_finished=lambda *_: pytest.fail("failed child exported"),
        ).execute(batch.id)

    assert batch.repo.task(batch.ids[0]).detail_visits == 1
    assert all(batch.repo.task(task_id).detail_visits == 0 for task_id in batch.ids[1:])
    assert batch.batches.batch(batch.id).status == "paused"
    assert batch.batches.batch(batch.id).stop_reason == "execution_error:ConnectionError"


def test_restarting_finished_batch_does_not_retry_natural_budget_exhaustion(batch):
    finish(batch.repo, batch.ids[0], "partial", "detail_budget_exhausted")
    for task_id in batch.ids[1:]:
        finish(batch.repo, task_id)
    runner = orchestrator(batch, lambda *_: pytest.fail("a finished attempt was restarted"))
    runner.execute(batch.id)
    assert batch.batches.batch(batch.id).status == "partial"
    assert batch.repo.task(batch.ids[0]).stop_reason == "detail_budget_exhausted"


def test_unknown_batch_never_starts_a_child(batch):
    with pytest.raises(ValueError, match="没有批次"):
        orchestrator(batch, lambda *_: pytest.fail("unknown batch started a child")).execute(
            "SYNTHETIC-missing-batch",
        )


def test_pause_state_failure_does_not_hide_original_child_failure(batch, monkeypatch):
    update = batch.batches.update

    def failing_update(batch_id, status, reason=None):
        if status == "paused":
            raise OSError("SYNTHETIC database unavailable during pause")
        return update(batch_id, status, reason)

    def execute(*_):
        raise ConnectionError("SYNTHETIC original child failure")

    monkeypatch.setattr(batch.batches, "update", failing_update)
    with pytest.raises(ConnectionError, match="SYNTHETIC original child failure"):
        orchestrator(batch, execute).execute(batch.id)
