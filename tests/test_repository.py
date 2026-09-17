"""Offline transaction tests; these SYNTHETIC rows are never real collection output."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select

from xhs_mobile.config import Policy
from xhs_mobile.domain import FieldValue, ParsedNote, Snapshot
from xhs_mobile.models import Base, Observation, Task
from xhs_mobile.repository import Repository


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield Repository(engine)
    engine.dispose()


def make_task(repo, keyword="SYNTHETIC keyword"):
    return repo.create_task(
        device_id="test-device",
        serial="SYNTHETIC-serial",
        session_ref="test-session",
        keyword=keyword,
        target=10,
        policy=Policy().model_dump(),
    )


def make_note(note_id="synthetic-note-01", complete=True):
    note = ParsedNote(
        fields={
            key: FieldValue(raw=f"SYNTHETIC {key}", status="present", method="ui")
            for key in ("title", "body", "author")
        },
        note_id=note_id,
        identity_source="synthetic_test",
        body_complete=None,
    )
    if not complete:
        note.fields["body"] = FieldValue(status="not_readable", reason="SYNTHETIC missing body")
    return note


def save(repo, task, run, note, observation_id=None):
    return repo.save_note(
        observation_id=observation_id or str(uuid4()),
        task_id=task.id,
        run_id=run,
        note=note,
        snapshot=Snapshot(xml="<synthetic/>", png=b"SYNTHETIC"),
        evidence=[{"metadata": {"source_kind": "synthetic"}}],
    )


def test_note_and_progress_are_atomic_and_retry_is_idempotent(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    first, saved = save(repo, task, run, make_note())
    assert saved
    second, saved = save(repo, task, run, make_note())
    assert not saved and second == first
    assert repo.task(task.id).eligible_count == repo.task(task.id).observation_count == 1
    assert len(repo.observations(task.id)) == 1


def test_commit_failure_rolls_back_record_and_progress(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")

    def fail_commit(session):
        raise RuntimeError("TEST_ONLY simulated process failure before commit")

    event.listen(repo.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            save(repo, task, run, make_note())
    finally:
        event.remove(repo.sessions, "before_commit", fail_commit)
    assert not repo.observations(task.id)
    assert repo.task(task.id).observation_count == 0
    save(repo, task, run, make_note())
    assert repo.task(task.id).eligible_count == 1


def test_missing_identity_is_never_silently_merged(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    save(repo, task, run, make_note(None))
    save(repo, task, run, make_note(None))
    status = repo.status(task.id)["tasks"][0]
    assert status["eligible_count"] == 2
    assert status["known_unique"] == 0
    assert status["identity_unconfirmed"] == 2
    assert len(status["possible_duplicates"][0]) == 2


def test_identical_text_with_different_platform_ids_kept(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    save(repo, task, run, make_note("synthetic-one"))
    save(repo, task, run, make_note("synthetic-two"))
    assert repo.status(task.id)["tasks"][0]["known_unique"] == 2


def test_incomplete_note_can_be_replaced_without_double_count(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    old, _ = save(repo, task, run, make_note(complete=False))
    assert repo.task(task.id).eligible_count == 0
    updated, _ = save(repo, task, run, make_note())
    assert updated == old
    assert repo.task(task.id).eligible_count == repo.task(task.id).observation_count == 1


def test_rejects_cross_task_run_and_observation_ids(repo):
    first, second = make_task(repo), make_task(repo, "another synthetic")
    run1 = repo.start_run(first.id, "synthetic-profile")
    run2 = repo.start_run(second.id, "synthetic-profile")
    obs, _ = save(repo, first, run1, make_note())
    with pytest.raises(ValueError, match="run does not belong"):
        save(repo, first, run2, make_note())
    with pytest.raises(ValueError, match="different task"):
        save(repo, second, run2, make_note(), observation_id=obs)
    assert repo.task(second.id).eligible_count == 0


def test_interrupted_operation_consumes_budget_on_restart(repo):
    task = make_task(repo)
    repo.start_run(task.id, "synthetic-profile")
    for _ in range(3):
        assert repo.begin_step(task.id, "action:click", 2)
        repo.start_run(task.id, "synthetic-profile")
    assert not repo.begin_step(task.id, "action:click", 2)
    assert repo.task(task.id).retry_counts["action:click"] == 3


def test_review_requires_quality_and_identity_and_preserves_rejection(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    obs, _ = save(repo, task, run, make_note(None))
    with pytest.raises(ValueError, match="identity"):
        repo.review(obs, verdict="accept", reviewer="TEST_ONLY", identity=None)
    repo.review(obs, verdict="accept", reviewer="TEST_ONLY", identity="same-note")
    assert repo.status(task.id)["tasks"][0]["human_verified_unique"] == 1
    repo.review(obs, verdict="reject", reviewer="TEST_ONLY", identity=None)
    assert repo.status(task.id)["tasks"][0]["human_verified_unique"] == 0
    incomplete, _ = save(repo, task, run, make_note("synthetic-incomplete", complete=False))
    with pytest.raises(ValueError, match="不能通过"):
        repo.review(incomplete, verdict="accept", reviewer="TEST_ONLY", identity=None)


def test_title_present_but_empty_not_eligible():
    note = make_note()
    note.fields["title"].raw = "   "
    assert not note.eligible


def test_statement_failure_leaves_counts_consistent(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "synthetic-profile")
    with pytest.raises(ValueError, match="evidence"):
        repo.save_note(
            observation_id=str(uuid4()),
            task_id=task.id,
            run_id=run,
            note=make_note(),
            snapshot=Snapshot(xml="<synthetic/>", png=b"SYNTHETIC"),
            evidence=[],
        )
    with repo.sessions() as session:
        assert session.get(Task, task.id).observation_count == 0
        assert list(session.scalars(select(Observation))) == []


def exhaust_capture(repo, task):
    run = repo.start_run(task.id, "synthetic-profile")
    for _ in range(3):
        assert repo.begin_step(task.id, "capture", 2)
        repo.fail_step(task.id, "capture", 2)
    repo.consume(task.id, "detail_visits", 100)
    repo.consume(task.id, "list_swipes", 50)
    repo.consume_retry(task.id, "connection:attempt", 3)
    repo.finish(task.id, run, "paused", "capture: persistent retry budget exhausted")


def test_legacy_grant_is_noop_and_never_adds_new_credits(repo):
    task = make_task(repo)
    exhaust_capture(repo, task)
    before = repo.task(task.id)
    request = str(uuid4())
    assert not repo.capture_retry_status(task.id)["exhausted"]
    assert repo.grant_read_retry(task.id, request)["checked"]
    assert repo.grant_read_retry(task.id, request)["credits"] == 0
    assert repo.read_retry_request_task(request) is None
    after = repo.task(task.id)
    for field in ("retry_counts", "target", "policy", "detail_visits", "list_swipes",
                  "observation_count", "eligible_count"):
        assert getattr(after, field) == getattr(before, field)


def test_read_streak_survives_interruption_but_success_resets_only_streak(repo):
    task = make_task(repo)
    repo.start_run(task.id, "SYNTHETIC initial")
    for _ in range(9):
        assert repo.begin_step(task.id, "capture", 2)
        assert repo.fail_step(task.id, "capture", 2)
    assert repo.begin_step(task.id, "read_state", 2)
    fresh = Repository(repo.engine)
    fresh.start_run(task.id, "SYNTHETIC interrupted restart")
    assert fresh.task(task.id).consecutive_read_failures == 10
    assert not fresh.begin_step(task.id, "capture", 2)
    assert fresh.task(task.id).retry_counts == {"capture": 9, "read_state": 1}
    fresh.complete_step(task.id, "capture")
    assert fresh.task(task.id).consecutive_read_failures == 0
    assert fresh.task(task.id).retry_counts == {"capture": 9, "read_state": 1}


def test_scattered_read_failures_do_not_accumulate_a_limit(repo):
    task = make_task(repo)
    repo.start_run(task.id, "SYNTHETIC initial")
    for _ in range(20):
        assert repo.begin_step(task.id, "read_state", 2)
        assert repo.fail_step(task.id, "read_state", 2)
        assert repo.begin_step(task.id, "capture", 2)
        repo.complete_step(task.id, "capture")
    assert repo.task(task.id).retry_counts["read_state"] == 20
    assert repo.task(task.id).consecutive_read_failures == 0
    assert repo.begin_step(task.id, "capture", 2)
    for _ in range(3):
        repo.fail_step(task.id, "action:click", 2)
    assert not repo.begin_step(task.id, "action:click", 2)


def test_explicit_threshold_continue_keeps_other_budgets(repo):
    task = make_task(repo)
    run = repo.start_run(task.id, "SYNTHETIC initial")
    for _ in range(10):
        repo.fail_step(task.id, "capture", 2)
    repo.consume(task.id, "detail_visits", 100)
    repo.consume_retry(task.id, "connection:attempt", 3)
    repo.finish(task.id, run, "paused", "consecutive_read_failures:10")
    repo.start_run(task.id, "SYNTHETIC explicit continue")
    fresh = repo.task(task.id)
    assert fresh.consecutive_read_failures == 0
    assert fresh.retry_counts == {"capture": 10, "connection:attempt": 1}
    assert fresh.detail_visits == 1


def test_legacy_historical_grant_remains_bound_and_diagnostic(repo):
    from xhs_mobile.models import TaskRetryGrant

    one, two = make_task(repo), make_task(repo)
    exhaust_capture(repo, one)
    exhaust_capture(repo, two)
    request = str(uuid4())
    with repo.sessions.begin() as session:
        session.add(TaskRetryGrant(request_id=request, task_id=one.id, step="capture", credits=3))
    assert repo.grant_read_retry(one.id, request)["already_granted"]
    assert repo.grant_read_retry(one.id, request)["credits"] == 0
    with pytest.raises(ValueError, match="另一任务"):
        repo.grant_read_retry(two.id, request)
    assert repo.capture_retry_status(one.id)["granted_credits"] == 3


@pytest.mark.parametrize("block", ["manual", "cooldown", "probe"])
def test_legacy_grant_cannot_clear_policy_or_add_other_budgets(repo, block):
    from datetime import timedelta

    from xhs_mobile.domain import utcnow

    task = make_task(repo)
    exhaust_capture(repo, task)
    kwargs = {"reason": "SYNTHETIC policy", "probe_used": block == "probe"}
    if block == "cooldown":
        kwargs["until"] = utcnow() + timedelta(minutes=30)
    repo.block(repo.task_scopes(task), **kwargs)
    before = repo.task(task.id).retry_counts
    with pytest.raises(ValueError, match="限制"):
        repo.grant_read_retry(task.id, str(uuid4()))
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert repo.task(task.id).retry_counts == before
    assert len(repo.policy_states(repo.task_scopes(task))) == 3


def test_legacy_grant_requires_pause_and_valid_request(repo):
    task = make_task(repo)
    exhaust_capture(repo, task)
    with pytest.raises(ValueError):
        repo.grant_read_retry(task.id, "not-a-uuid")
    repo.start_run(task.id, "SYNTHETIC restart")
    with pytest.raises(ValueError, match="已暂停"):
        repo.grant_read_retry(task.id, str(uuid4()))
