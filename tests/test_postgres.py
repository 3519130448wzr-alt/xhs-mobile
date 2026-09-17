"""Opt-in PostgreSQL tests; every record and snapshot here is SYNTHETIC.

Only TEST_DATABASE_URL can enable these tests. The URL and the connected database
must both identify a test database before any DDL runs. Each test owns a fresh
random schema; cleanup never drops public tables or another test's schema.
"""

import os
from importlib.resources import files
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    event,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DataError, IntegrityError
from sqlalchemy.pool import NullPool

from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import Policy
from xhs_mobile.domain import FieldValue, ParsedNote, Snapshot, utcnow
from xhs_mobile.models import Batch, BatchItem, Observation, Run, Task
from xhs_mobile.repository import Repository, identifier

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="Set TEST_DATABASE_URL to an isolated PostgreSQL database containing 'test'",
    ),
]

EXPECTED_TABLES = {
    "alembic_version",
    "tasks",
    "runs",
    "observations",
    "events",
    "policy_states",
    "batches",
    "batch_items",
    "task_retry_grants",
    "quality_assessment_audits",
}


def configured_test_database_url():
    """Validate routing before creating an engine; never include credentials in errors."""
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is not configured")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        pytest.skip("TEST_DATABASE_URL is not a valid SQLAlchemy URL")
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or "").lower():
        pytest.skip("Refusing a URL that does not explicitly name a PostgreSQL test database")
    if {key.lower() for key in url.query} & {"dbname", "database", "service"}:
        pytest.skip("Refusing database-routing overrides in TEST_DATABASE_URL")
    return url


def migration_config(connection):
    config = Config()
    config.set_main_option("script_location", str(files("xhs_mobile").joinpath("migrations")))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def pg_engine():
    url = configured_test_database_url()
    admin = create_engine(url, connect_args={"connect_timeout": 5}, poolclass=NullPool)
    schema = f"xhs_test_{uuid4().hex}"
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            actual_database = connection.scalar(text("SELECT current_database()"))
        if actual_database != url.database or "test" not in actual_database.lower():
            pytest.skip("Connected database differs from the approved test database; no DDL run")
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"},
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            assert connection.scalar(text("SELECT current_database()")) == actual_database
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def pg_repository(pg_engine):
    with pg_engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
    return Repository(pg_engine)


def make_task(repo, *, keyword="SYNTHETIC 测试关键词"):
    task = repo.create_task(
        device_id="synthetic-pg-device",
        serial="SYNTHETIC-PG-SERIAL",
        session_ref="synthetic-pg-session",
        keyword=keyword,
        target=10,
        policy={},
    )
    return task, repo.start_run(task.id, "0" * 64)


def note(note_id="synthetic-note-1", *, complete=True):
    return ParsedNote(
        fields={
            name: FieldValue(raw=value if complete or name != "body" else None,
                             status="present" if complete or name != "body" else "not_readable",
                             method="ui")
            for name, value in {
                "title": "SYNTHETIC 标题",
                "body": "SYNTHETIC 完整正文，不是真实数据。",
                "author": "SYNTHETIC 作者",
            }.items()
        },
        note_id=note_id,
        identity_source="synthetic_fixture" if note_id is not None else None,
        body_complete=complete,
    )


def save(repo, task, run_id, parsed=None, *, observation_id=None):
    return repo.save_note(
        observation_id=observation_id or identifier(),
        task_id=task.id,
        run_id=run_id,
        note=parsed or note(),
        snapshot=Snapshot(
            xml='<hierarchy synthetic="true"/>',
            png=b"SYNTHETIC PostgreSQL test bytes",
            metadata={"source_kind": "synthetic", "app_version": "SYNTHETIC-1"},
        ),
        evidence=[{"source_kind": "synthetic", "label": "postgres-transaction-fixture"}],
    )


def counts(repo, task_id):
    task = repo.task(task_id)
    with repo.engine.connect() as connection:
        rows = connection.scalar(
            select(func.count()).select_from(Observation).where(Observation.task_id == task_id)
        )
    return task.observation_count, task.eligible_count, rows


def test_frozen_initial_migration_upgrade_downgrade_upgrade_and_model_check(pg_engine):
    with pg_engine.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == EXPECTED_TABLES
        command.check(config)
        assert "pending_step" in {
            column["name"] for column in inspect(connection).get_columns("tasks")
        }
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == EXPECTED_TABLES
        command.check(config)


def legacy_fixture(engine, revision):
    """Populate frozen old columns directly; current ORM requires current schema."""
    task_id, run_id, observation_id = identifier(), identifier(), identifier()
    now = utcnow()
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), revision)
        meta = MetaData()
        tasks = Table("tasks", meta, autoload_with=connection)
        runs = Table("runs", meta, autoload_with=connection)
        observations = Table("observations", meta, autoload_with=connection)
        connection.execute(tasks.insert().values(
            id=task_id, device_id="synthetic-legacy", serial_hash="0" * 64,
            session_ref="synthetic-legacy", keyword="SYNTHETIC legacy", target=10,
            mode="search", status="paused",
            stop_reason="device_error:step_budget_exhausted:capture",
            pause_requested=False, policy=Policy().model_dump(), detail_visits=12,
            list_swipes=7, no_progress=0, retry_counts={"capture": 3, "connection:attempt": 2},
            pending_step=None, observation_count=1, eligible_count=1,
            created_at=now, updated_at=now,
        ))
        connection.execute(runs.insert().values(
            id=run_id, task_id=task_id, profile_hash="0" * 64, started_at=now,
            ended_at=now, status="paused",
        ))
        data = note().to_dict()
        data.pop("quality_revision")
        connection.execute(observations.insert().values(
            id=observation_id, task_id=task_id, run_id=run_id,
            device_id="synthetic-legacy", session_ref="synthetic-legacy",
            platform="xiaohongshu", schema_version=1, note_id=None, canonical_url=None,
            identity_source=None, fingerprint="1" * 64, eligible=True, data=data,
            evidence=[{"source_kind": "synthetic", "label": "legacy-migration"}],
            app_version="SYNTHETIC-1", captured_at=now,
        ))
        before_task = dict(connection.execute(select(tasks)).mappings().one())
        before_row = dict(connection.execute(select(observations)).mappings().one())
    return task_id, before_task, before_row


def assert_legacy_unchanged(engine, task_id, before_task, before_row):
    with engine.connect() as connection:
        meta = MetaData()
        tasks = Table("tasks", meta, autoload_with=connection)
        observations = Table("observations", meta, autoload_with=connection)
        after = dict(connection.execute(
            select(tasks).where(tasks.c.id == task_id)).mappings().one())
        assert {key: after[key] for key in before_task} == before_task
        assert dict(connection.execute(select(observations).where(
            observations.c.id == before_row["id"])).mappings().one()) == before_row


def test_batch_migration_preserves_existing_task_evidence_and_progress(pg_engine):
    task_id, before_task, before_row = legacy_fixture(pg_engine, "0001")
    with pg_engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        command.check(migration_config(connection))
    assert_legacy_unchanged(pg_engine, task_id, before_task, before_row)
    repo = Repository(pg_engine)
    batch_id = BatchRepository(repo).create_batch(
        device_id="SYNTHETIC-batch-device", serial="SYNTHETIC-serial",
        session_ref="SYNTHETIC-session", keywords=["SYNTHETIC 甲", "SYNTHETIC 乙"],
        target=10, policy=Policy().model_dump(),
    )
    child_ids = BatchRepository(repo).task_ids(batch_id)
    with pg_engine.begin() as connection:
        command.downgrade(migration_config(connection), "0001")
    assert_legacy_unchanged(pg_engine, task_id, before_task, before_row)
    with pg_engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        command.check(migration_config(connection))
    assert all(repo.task(child_id).status == "pending" for child_id in child_ids)
    assert BatchRepository(repo).status()["batches"] == []
    assert_legacy_unchanged(pg_engine, task_id, before_task, before_row)


def test_batch_transaction_after_child_flush_rolls_back_batch_and_tasks(pg_repository):
    repo = pg_repository
    batches = BatchRepository(repo)

    def interrupt_after_child_flush(session, flush_context):
        raise RuntimeError("SYNTHETIC interruption after child task INSERTs")

    event.listen(repo.sessions, "after_flush_postexec", interrupt_after_child_flush)
    try:
        with pytest.raises(RuntimeError, match="SYNTHETIC"):
            batches.create_batch(
                device_id="SYNTHETIC-device", serial="SYNTHETIC-serial",
                session_ref="SYNTHETIC-session", keywords=["SYNTHETIC 甲", "SYNTHETIC 乙"],
                target=10, policy=Policy().model_dump(),
            )
    finally:
        event.remove(repo.sessions, "after_flush_postexec", interrupt_after_child_flush)
    with repo.sessions() as session:
        assert all(
            session.scalar(select(func.count()).select_from(model)) == 0
            for model in (Task, Batch, BatchItem)
        )


def test_batch_item_cannot_reassign_task_to_another_batch(pg_repository):
    repo = pg_repository
    batches = BatchRepository(repo)
    args = {
        "device_id": "SYNTHETIC-device", "serial": "SYNTHETIC-serial",
        "session_ref": "SYNTHETIC-session", "keywords": ["SYNTHETIC 甲"],
        "target": 10, "policy": Policy().model_dump(),
    }
    first, second = batches.create_batch(**args), batches.create_batch(**args)
    child_id = batches.task_ids(first)[0]
    with pytest.raises(IntegrityError) as failure, repo.engine.begin() as connection:
        connection.execute(
            insert(BatchItem).values(batch_id=second, position=1, task_id=child_id)
        )
    assert failure.value.orig.sqlstate == "23505"
    assert failure.value.orig.diag.constraint_name == "uq_batch_item_task"
    assert batches.task_ids(first) == [child_id]
    assert len(batches.task_ids(second)) == 1


def test_exception_after_sql_flush_rolls_back_observation_and_progress(pg_repository):
    repo = pg_repository
    task, run_id = make_task(repo)
    observation_id = identifier()

    class SyntheticInterruption(RuntimeError):
        pass

    def interrupt_after_flush(session, flush_context):
        raise SyntheticInterruption("SYNTHETIC interruption after SQL flush, before commit")

    event.listen(repo.sessions.class_, "after_flush_postexec", interrupt_after_flush)
    try:
        with pytest.raises(SyntheticInterruption):
            save(repo, task, run_id, observation_id=observation_id)
    finally:
        event.remove(repo.sessions.class_, "after_flush_postexec", interrupt_after_flush)
    assert counts(repo, task.id) == (0, 0, 0)
    saved_id, changed = save(repo, task, run_id, observation_id=observation_id)
    assert (saved_id, changed) == (observation_id, True)
    assert counts(repo, task.id) == (1, 1, 1)


def test_database_rejected_insert_does_not_advance_existing_progress(pg_repository):
    repo = pg_repository
    task, run_id = make_task(repo)
    save(repo, task, run_id)
    with pytest.raises(DataError) as failure:
        save(repo, task, run_id, note("x" * 257))
    assert failure.value.orig.sqlstate == "22001"
    assert counts(repo, task.id) == (1, 1, 1)


def test_postgres_unique_task_note_constraint_rolls_back_counter_update(pg_repository):
    repo = pg_repository
    task, run_id = make_task(repo)
    save(repo, task, run_id)
    with repo.engine.connect() as connection:
        duplicate = dict(connection.execute(select(Observation.__table__)).mappings().one())
    duplicate["id"] = identifier()
    with pytest.raises(IntegrityError) as failure, repo.engine.begin() as connection:
        connection.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                observation_count=Task.observation_count + 1,
                eligible_count=Task.eligible_count + 1,
            )
        )
        connection.execute(insert(Observation).values(**duplicate))
    assert failure.value.orig.sqlstate == "23505"
    assert failure.value.orig.diag.constraint_name == "uq_task_note"
    assert counts(repo, task.id) == (1, 1, 1)


def test_trusted_note_id_is_idempotent_per_task_but_can_appear_in_another_task(pg_repository):
    repo = pg_repository
    first, first_run = make_task(repo)
    second, second_run = make_task(repo, keyword="SYNTHETIC 第二个关键词")
    first_id, changed = save(repo, first, first_run)
    assert changed
    assert save(repo, first, first_run) == (first_id, False)
    second_id, changed = save(repo, second, second_run)
    assert changed and second_id != first_id
    assert counts(repo, first.id) == counts(repo, second.id) == (1, 1, 1)


def test_null_note_ids_preserve_multiple_same_fingerprint_observations(pg_repository):
    repo = pg_repository
    task, run_id = make_task(repo)
    first_id, _ = save(repo, task, run_id, note(None))
    second_id, _ = save(repo, task, run_id, note(None))
    assert first_id != second_id
    assert save(repo, task, run_id, note(None), observation_id=first_id) == (first_id, False)
    assert counts(repo, task.id) == (2, 2, 2)
    status = repo.status(task.id)["tasks"][0]
    assert status["known_unique"] == 0
    assert status["identity_unconfirmed"] == 2
    assert sorted(status["possible_duplicates"][0]) == sorted([first_id, second_id])


def test_incomplete_note_upgrade_preserves_observation_count(pg_repository):
    repo = pg_repository
    task, run_id = make_task(repo)
    first_id, _ = save(repo, task, run_id, note(complete=False))
    assert counts(repo, task.id) == (1, 0, 1)
    assert save(repo, task, run_id, note(complete=True)) == (first_id, True)
    assert counts(repo, task.id) == (1, 1, 1)
    assert save(repo, task, run_id) == (first_id, False)
    assert counts(repo, task.id) == (1, 1, 1)


def test_interrupted_step_budget_and_run_status_survive_a_new_run(pg_repository):
    repo = pg_repository
    task, previous_run = make_task(repo)
    assert repo.begin_step(task.id, "capture", retry_limit=2)
    assert repo.task(task.id).pending_step == "capture"
    new_run = repo.start_run(task.id, "1" * 64)
    restored = repo.task(task.id)
    assert restored.pending_step is None
    assert restored.retry_counts["capture"] == 1
    with repo.sessions() as session:
        old = session.get(Run, previous_run)
        new = session.get(Run, new_run)
        assert old.status == "interrupted" and old.ended_at is not None
        assert new.status == "running"


def test_read_retry_migration_preserves_0002_tasks_and_legacy_parameter_is_noop(pg_engine):
    from xhs_mobile.models import TaskRetryGrant

    task_id, before_task, before_row = legacy_fixture(pg_engine, "0002")
    with pg_engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        command.check(migration_config(connection))
    assert_legacy_unchanged(pg_engine, task_id, before_task, before_row)
    repo = Repository(pg_engine)
    task = repo.task(task_id)
    assert task.consecutive_read_failures == task.consecutive_no_progress == 0
    request = str(uuid4())
    assert not repo.grant_read_retry(task_id, request)["granted"]
    assert not repo.grant_read_retry(task_id, request)["granted"]
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskRetryGrant)) == 0
    assert_legacy_unchanged(pg_engine, task_id, before_task, before_row)
    assert repo.capture_retry_status(task_id)["available_attempts"] is None
    assert repo.begin_step(task_id, "capture", 2)
    repo.start_run(task_id, "SYNTHETIC crash reconciliation")
    assert repo.task(task_id).retry_counts == {"capture": 4, "connection:attempt": 2}
    assert repo.task(task_id).consecutive_read_failures == 1
    assert repo.capture_retry_status(task_id)["available_attempts"] is None
