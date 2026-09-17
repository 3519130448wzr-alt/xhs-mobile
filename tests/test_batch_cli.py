"""SYNTHETIC CLI integration: SQLite storage and fake runners, never an Android connection."""

import csv
import json
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from xhs_mobile import batch_exports
from xhs_mobile import cli as module
from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import DeviceConfig, Policy, Settings
from xhs_mobile.domain import DeviceBusy, FieldValue, ParsedNote, Snapshot
from xhs_mobile.locking import DeviceLock
from xhs_mobile.models import Base, Task
from xhs_mobile.repository import Repository, identifier

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(
        state_dir=tmp_path / "state", profile_path=tmp_path / "SYNTHETIC-profile.toml",
        devices={"synthetic": DeviceConfig(
            serial_env="XHS_SYNTHETIC_SERIAL", session_ref="SYNTHETIC-session",
            app_package="synthetic.package",
        )},
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'SYNTHETIC.db'}")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    monkeypatch.setenv("XHS_SYNTHETIC_SERIAL", "SYNTHETIC-serial")
    monkeypatch.setenv("XHS_DATABASE_URL", "postgresql+psycopg://SYNTHETIC@localhost/SYNTHETIC_test")
    monkeypatch.setattr(module, "settings_for", lambda ctx: settings)
    monkeypatch.setattr(module, "load_profile", Mock(
        return_value=SimpleNamespace(app_package="synthetic.package"),
    ))
    connect = Mock(return_value=repo)
    monkeypatch.setattr(module.Repository, "connect", connect)
    adapter = Mock(serial="SYNTHETIC-serial")
    monkeypatch.setattr(module, "device_for", Mock(return_value=adapter))
    yield SimpleNamespace(settings=settings, repo=repo, adapter=adapter, connect=connect)
    engine.dispose()


def create(env, keywords=None):
    return BatchRepository(env.repo).create_batch(
        device_id="synthetic", serial="SYNTHETIC-serial", session_ref="SYNTHETIC-session",
        keywords=keywords or ["SYNTHETIC 甲", "SYNTHETIC 乙"], target=10,
        policy=Policy().model_dump(),
    )


def json_messages(stdout):
    result = []
    remaining = stdout.lstrip()
    while remaining:
        value, end = json.JSONDecoder().raw_decode(remaining)
        result.append(value)
        remaining = remaining[end:].lstrip()
    return result


def fake_runner(env, monkeypatch, *, status="collected_awaiting_review", reason="synthetic_done"):
    calls = []

    class SyntheticRunner:
        def __init__(self, *, repository, device, evidence, profile, stop_requested):
            self.repo = repository
            self.last_action = None
            self.stopped = stop_requested
            assert device is env.adapter

        def execute(self, task_id, *, acknowledge=False):
            with pytest.raises(DeviceBusy), DeviceLock("SYNTHETIC-serial", env.settings.state_dir):
                pytest.fail("Batch must retain the physical device lock across every child")
            calls.append((task_id, acknowledge, self.last_action))
            self.last_action = 123.0
            run_id = self.repo.start_run(task_id, "SYNTHETIC-profile")
            self.repo.finish(task_id, run_id, status, reason)

    monkeypatch.setattr(module, "Runner", SyntheticRunner)
    return calls


def save(env, task_id):
    run_id = env.repo.start_run(task_id, "SYNTHETIC-profile")
    return env.repo.save_note(
        observation_id=identifier(), task_id=task_id, run_id=run_id,
        note=ParsedNote(fields={
            key: FieldValue(raw=f"SYNTHETIC {key}", status="present", method="ui")
            for key in ("title", "body", "author")
        }, body_complete=True),
        snapshot=Snapshot(xml="<synthetic/>", png=b"SYNTHETIC"),
        evidence=[{"source_kind": "synthetic"}],
    )


def test_batch_help_lists_routes_without_device_or_database():
    result = runner.invoke(module.app, ["batch", "--help"])
    assert result.exit_code == 0
    for command in ("run", "resume", "status", "pause", "export"):
        assert command in result.stdout


def test_run_preflight_rejects_uncalibrated_profile_before_connections(env, monkeypatch):
    monkeypatch.setattr(module, "load_profile", Mock(
        side_effect=ValueError("SYNTHETIC unverified"),
    ))
    result = runner.invoke(module.app, [
        "batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC 甲",
    ])
    assert result.exit_code == 2 and "unverified" in result.stdout
    env.connect.assert_not_called()
    module.device_for.assert_not_called()
    assert env.repo.status()["tasks"] == []


def test_run_routes_repeated_keywords_and_holds_lock_across_children(env, monkeypatch):
    calls = fake_runner(env, monkeypatch)
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    result = runner.invoke(module.app, [
        "batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC 甲",
        "--keyword", "SYNTHETIC 乙", "--limit", "2",
    ])
    assert result.exit_code == 0, result.stdout
    messages = json_messages(result.stdout)
    batch_id = messages[0]["batch_id"]
    tasks = BatchRepository(env.repo).status(batch_id)["batches"][0]["tasks"]
    assert [task["keyword"] for task in tasks] == ["SYNTHETIC 甲", "SYNTHETIC 乙"]
    assert [task["target"] for task in tasks] == [2, 2]
    assert [call[0] for call in calls] == [task["id"] for task in tasks]
    assert [call[2] for call in calls] == [None, 123.0]
    assert handlers == {number: signal.getsignal(number) for number in handlers}
    with DeviceLock("SYNTHETIC-serial", env.settings.state_dir):
        pass
    env.adapter.assert_not_called()


def test_busy_device_keeps_durable_pending_batch_without_starting_run(env, monkeypatch):
    calls = fake_runner(env, monkeypatch)
    with DeviceLock("SYNTHETIC-serial", env.settings.state_dir):
        result = runner.invoke(module.app, [
            "batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC 甲",
        ])
    assert result.exit_code == 2
    messages = json_messages(result.stdout)
    assert messages[0]["event"] == "batch_created"
    assert messages[1]["error"] == "DeviceBusy"
    batch = BatchRepository(env.repo).status(messages[0]["batch_id"])["batches"][0]
    assert batch["status"] == "pending" and batch["tasks"][0]["status"] == "pending"
    assert calls == []


@pytest.mark.parametrize("binding", ["serial", "session", "child"])
def test_resume_rejects_changed_binding_before_running_phone(env, monkeypatch, binding):
    batch_id = create(env)
    if binding == "serial":
        env.adapter.serial = "SYNTHETIC-changed"
    elif binding == "session":
        env.settings.devices["synthetic"].session_ref = "SYNTHETIC-changed"
    else:
        with env.repo.sessions.begin() as session:
            child = session.get(Task, BatchRepository(env.repo).task_ids(batch_id)[0])
            child.device_id = "SYNTHETIC-changed"
    calls = fake_runner(env, monkeypatch)
    result = runner.invoke(module.app, ["batch", "resume", "--batch", batch_id])
    assert result.exit_code == 2
    assert "批次" in result.stdout
    assert calls == []
    assert BatchRepository(env.repo).batch(batch_id).status == "pending"


def test_resume_reuses_existing_tasks_and_reports_partial_exit(env, monkeypatch):
    batch_id = create(env)
    task_ids = BatchRepository(env.repo).task_ids(batch_id)
    run_id = env.repo.start_run(task_ids[0], "SYNTHETIC-profile")
    env.repo.finish(task_ids[0], run_id, "collected_awaiting_review", "SYNTHETIC done")
    calls = fake_runner(env, monkeypatch, status="paused", reason="unknown_page")
    result = runner.invoke(module.app, ["batch", "resume", "--batch", batch_id, "--acknowledge"])
    assert result.exit_code == 3, result.stdout
    assert calls == [(task_ids[1], True, None)]
    assert BatchRepository(env.repo).task_ids(batch_id) == task_ids
    assert len(env.repo.status()["tasks"]) == 2
    report = json_messages(result.stdout)[-1]["batches"][0]
    assert report["status"] == "paused" and report["stop_reason"] == "unknown_page"


def test_pause_and_status_routes_are_device_free_and_preserve_progress(env):
    batch_id = create(env)
    task_id = BatchRepository(env.repo).task_ids(batch_id)[0]
    env.repo.start_run(task_id, "SYNTHETIC-profile")
    env.repo.consume(task_id, "detail_visits", 100)
    result = runner.invoke(module.app, ["batch", "pause", "--batch", batch_id])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["pause_requested"] is True
    result = runner.invoke(module.app, ["batch", "status", "--batch", batch_id])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["batches"][0]["pause_requested"] is True
    assert report["batches"][0]["tasks"][0]["pause_requested"] is True
    assert report["batches"][0]["tasks"][0]["detail_visits"] == 1
    module.device_for.assert_not_called()


def test_bundle_export_uses_one_frozen_record_list_and_excludes_other_batches(
    env, tmp_path, monkeypatch,
):
    batch_id, other = create(env), create(env, ["SYNTHETIC outsider"])
    task_ids = BatchRepository(env.repo).task_ids(batch_id)
    save(env, task_ids[0])
    save(env, BatchRepository(env.repo).task_ids(other)[0])
    original = batch_exports.export_bundle
    snapshots = []

    def after_select(records, batch, output):
        snapshots.append(records)
        save(env, task_ids[1])  # New DB state after the export SELECT must not split the two files.
        return original(records, batch, output)

    monkeypatch.setattr(batch_exports, "export_bundle", after_select)
    output = tmp_path / "SYNTHETIC-export"
    result = runner.invoke(module.app, [
        "batch", "export", "--batch", batch_id, "--format", "bundle", "--output", str(output),
    ])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["count"] == 1
    assert len(snapshots) == 1 and snapshots[0][0]["task_id"] == task_ids[0]
    jsonl = [json.loads(line) for line in (output / "notes.jsonl").read_text().splitlines()]
    with (output / "notes.csv").open(newline="") as stream:
        csv_records = [json.loads(row["record_json"]) for row in csv.DictReader(stream)]
    assert jsonl == csv_records == snapshots[0]
    assert len(env.repo.observations(task_ids[1])) == 1
    module.device_for.assert_not_called()


def test_export_invalid_format_does_not_create_output(env, tmp_path):
    batch_id = create(env)
    output = tmp_path / "SYNTHETIC-invalid"
    result = runner.invoke(module.app, [
        "batch", "export", "--batch", batch_id, "--format", "xlsx", "--output", str(output),
    ])
    assert result.exit_code == 2
    assert not output.exists()
    module.device_for.assert_not_called()


@pytest.mark.parametrize("target", [100, 101, 500])
@pytest.mark.parametrize("kind", ["run", "batch"])
def test_cli_targets_above_previous_limit_persist_scaled_policy(env, monkeypatch, target, kind):
    if kind == "run":
        monkeypatch.setattr(module, "execute_task", Mock())
        args = ["run", "--device", "synthetic", "--keyword", "SYNTHETIC target"]
    else:
        fake_runner(env, monkeypatch)
        args = ["batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC target"]
    result = runner.invoke(module.app, [*args, "--limit", str(target)])
    assert result.exit_code == 0, result.stdout
    tasks = env.repo.status()["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["target"] == target
    assert task["policy"]["max_detail_visits"] == max(100, 3 * target)
    assert task["policy"]["max_list_swipes"] == max(50, (3 * target + 1) // 2)


@pytest.mark.parametrize("kind", ["run", "batch"])
def test_cli_rejects_501_before_creating_any_task(env, kind):
    args = ["run", "--device", "synthetic", "--keyword", "SYNTHETIC target"]
    if kind == "batch":
        args.insert(0, "batch")
    result = runner.invoke(module.app, [*args, "--limit", "501"])
    assert result.exit_code == 2
    assert env.repo.status()["tasks"] == []
    env.connect.assert_not_called()


def test_resume_accepts_explicit_uuid_and_forwards_ack_without_replacing_task(env, monkeypatch):
    from uuid import uuid4

    batch = create(env)
    task_id = BatchRepository(env.repo).task_ids(batch)[0]
    execute = Mock()
    monkeypatch.setattr(module, "execute_task", execute)
    request = str(uuid4())
    result = runner.invoke(module.app, ["resume", "--task", task_id,
                                        "--retry-read-request-id", request, "--acknowledge"])
    assert result.exit_code == 0, result.stdout
    execute.assert_called_once_with(env.settings, env.repo, task_id, acknowledge=True,
                                    retry_read_request_id=request)
    assert len(env.repo.status()["tasks"]) == 2


def test_batch_explicit_read_retry_only_first_unfinished_child_under_device_lock(env, monkeypatch):
    from uuid import uuid4

    batch = create(env, ["SYNTHETIC finished", "SYNTHETIC retry", "SYNTHETIC next"])
    ids = BatchRepository(env.repo).task_ids(batch)
    run_id = env.repo.start_run(ids[0], "SYNTHETIC done")
    env.repo.finish(ids[0], run_id, "collected_awaiting_review", "target_collected")
    calls = fake_runner(env, monkeypatch)
    base = module.Runner
    grants = []

    class RetryRunner(base):
        def check_and_grant_read_retry(self, task_id, request_id, *, acknowledge=False):
            with pytest.raises(DeviceBusy), DeviceLock("SYNTHETIC-serial", env.settings.state_dir):
                pytest.fail("Recovery diagnostics must hold the same device lock")
            grants.append((task_id, request_id, acknowledge))
            return {"granted": True, "credits": 3}

    monkeypatch.setattr(module, "Runner", RetryRunner)
    request = str(uuid4())
    result = runner.invoke(module.app, ["batch", "resume", "--batch", batch,
                                        "--retry-read-request-id", request, "--acknowledge"])
    assert result.exit_code == 0, result.stdout
    assert grants == [(ids[1], request, True)]
    assert [call[0] for call in calls] == ids[1:]
    assert [call[1] for call in calls] == [True, False]


def test_bad_retry_request_id_rejected_without_device_or_database(env):
    result = runner.invoke(module.app, ["resume", "--task", "SYNTHETIC",
                                        "--retry-read-request-id", "not-a-uuid"])
    assert result.exit_code == 2
    env.connect.assert_not_called()


def test_single_retry_diagnostics_and_resume_share_exclusive_device_lock(env, monkeypatch):
    from uuid import uuid4

    batch = create(env)
    task_id = BatchRepository(env.repo).task_ids(batch)[0]
    actions = []

    class RetryRunner:
        def __init__(self, *, repository, device, evidence, profile, stop_requested):
            self.repo = repository

        def assert_locked(self):
            with pytest.raises(DeviceBusy), DeviceLock("SYNTHETIC-serial", env.settings.state_dir):
                pytest.fail("A second owner must not enter between check and execution")

        def check_and_grant_read_retry(self, current, request_id, *, acknowledge=False):
            self.assert_locked()
            actions.append(("check", current, request_id, acknowledge))
            return {"granted": True, "credits": 3}

        def execute(self, current, *, acknowledge=False):
            self.assert_locked()
            actions.append(("execute", current))
            run_id = self.repo.start_run(current, "SYNTHETIC profile")
            self.repo.finish(current, run_id, "collected_awaiting_review", "SYNTHETIC done")
            return self.repo.status(current)

    monkeypatch.setattr(module, "Runner", RetryRunner)
    request = str(uuid4())
    result = runner.invoke(module.app, ["resume", "--task", task_id,
                                        "--retry-read-request-id", request])
    assert result.exit_code == 0, result.stdout
    assert actions == [("check", task_id, request, False), ("execute", task_id)]
