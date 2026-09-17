"""SYNTHETIC desktop lifetime tests; no Android or production database access."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from xhs_mobile import cli
from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import DeviceConfig, Settings
from xhs_mobile.control import (
    CONTROL_FD_ENV,
    DesktopControl,
    desktop_controlled,
    execution_stop,
)
from xhs_mobile.domain import DeviceUnavailable
from xhs_mobile.models import Base
from xhs_mobile.profile import load_profile
from xhs_mobile.repository import Repository

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def channel(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(CONTROL_FD_ENV, str(read_fd))
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.parametrize("value", ["", "0", "1", "2", "-1", "abc", "3.0", "٣"])
def test_invalid_control_value_is_rejected_without_touching_stdio(value):
    before = [os.fstat(fd) for fd in (0, 1, 2)]
    with pytest.raises(ValueError, match="descriptor"):
        DesktopControl(value)
    assert [os.fstat(fd) for fd in (0, 1, 2)] == before


def test_wrong_descriptor_type_and_pipe_write_end_are_not_closed(tmp_path, channel):
    with (tmp_path / "SYNTHETIC.txt").open("wb") as stream:
        with pytest.raises(ValueError, match="read-only pipe"):
            DesktopControl(str(stream.fileno()))
        os.fstat(stream.fileno())
    with pytest.raises(ValueError, match="read-only pipe"):
        DesktopControl(str(channel[1]))
    os.fstat(channel[1])


def test_closed_descriptor_fails_closed():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    with pytest.raises(ValueError, match="unavailable"):
        DesktopControl(str(read_fd))


@pytest.mark.parametrize("pause_kind", ["eof", "byte"])
def test_pipe_notifies_existing_stop_event_and_closes_owned_copy(channel, pause_kind):
    control = DesktopControl(str(channel[0]))
    owned = control._fd
    with control:
        assert control.stopped() is False
        assert owned is not None and not os.get_inheritable(owned)
        if pause_kind == "eof":
            os.close(channel[1])
        else:
            os.write(channel[1], b"pause")
        assert control.stop.wait(2), "background reader must signal without an active runner"
        assert control.stopped()
    with pytest.raises(OSError):
        os.fstat(owned)
    os.fstat(channel[0])  # the environment does not transfer unrelated fd ownership
    assert not control._thread.is_alive()


def test_no_environment_preserves_ordinary_cli_control(monkeypatch):
    monkeypatch.delenv(CONTROL_FD_ENV, raising=False)

    @desktop_controlled
    def synthetic_command():
        event, stopped = execution_stop()
        assert isinstance(event, threading.Event)
        assert not stopped()
        event.set()
        assert stopped()

    synthetic_command()
    event, stopped = execution_stop()
    assert not stopped(), "control context must not leak between CLI invocations"
    assert not any(t.name == "xhs-desktop-control" for t in threading.enumerate())


@pytest.mark.parametrize("command", [
    ["run", "--device", "synthetic", "--keyword", "SYNTHETIC"],
    ["collect-current", "--device", "synthetic"],
    ["resume", "--task", "SYNTHETIC"],
    ["batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC"],
    ["batch", "resume", "--batch", "SYNTHETIC"],
])
def test_parent_dead_before_start_prevents_configuration_database_and_device(
    channel, monkeypatch, command,
):
    os.close(channel[1])
    setup = Mock(side_effect=AssertionError("SYNTHETIC: setup must not begin"))
    monkeypatch.setattr(cli, "settings_for", setup)
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 2, result.stdout
    assert "DesktopParentGone" in result.stdout
    setup.assert_not_called()


@pytest.fixture
def synthetic_environment(tmp_path, monkeypatch):
    profile = load_profile(ROOT / "tests/fixtures/synthetic_profile.toml", require_verified=False)
    settings = Settings(
        state_dir=tmp_path / "state", profile_path=ROOT / "tests/fixtures/synthetic_profile.toml",
        devices={"synthetic": DeviceConfig(
            serial_env="XHS_SYNTHETIC_SERIAL", session_ref="SYNTHETIC-session",
            app_package=profile.app_package,
        )},
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'SYNTHETIC.db'}")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    monkeypatch.setenv("XHS_SYNTHETIC_SERIAL", "SYNTHETIC-serial")
    monkeypatch.setenv("XHS_DATABASE_URL", "postgresql+psycopg://SYNTHETIC/SYNTHETIC")
    monkeypatch.setattr(cli, "settings_for", lambda _: settings)
    monkeypatch.setattr(cli, "load_profile", Mock(return_value=profile))
    monkeypatch.setattr(cli.Repository, "connect", Mock(return_value=repo))
    phone = Mock(serial="SYNTHETIC-serial")
    monkeypatch.setattr(cli, "device_for", Mock(return_value=phone))
    yield repo, phone
    engine.dispose()


def test_parent_dies_during_early_database_setup_creates_no_task(
    channel, monkeypatch, synthetic_environment,
):
    repo, phone = synthetic_environment

    def connected(_):
        os.close(channel[1])
        return repo

    monkeypatch.setattr(cli.Repository, "connect", connected)
    result = CliRunner().invoke(cli.app, ["collect-current", "--device", "synthetic"])
    assert result.exit_code == 2, result.stdout
    assert repo.status()["tasks"] == []
    phone.assert_not_called()
    cli.device_for.assert_not_called()


def test_new_task_id_is_emitted_before_device_setup_failure(
    channel, monkeypatch, synthetic_environment,
):
    repo, _ = synthetic_environment
    monkeypatch.setattr(cli, "device_for", Mock(side_effect=DeviceUnavailable("SYNTHETIC")))
    result = CliRunner().invoke(cli.app, ["collect-current", "--device", "synthetic"])
    assert result.exit_code == 2, result.stdout
    task = repo.status()["tasks"][0]
    assert '"event": "task_created"' in result.stdout
    assert task["id"] in result.stdout
    assert "run_started" not in result.stdout
    assert task["status"] == "pending"


@pytest.mark.parametrize("batch", [False, True])
def test_parent_dies_after_commit_pauses_without_any_phone_operation(
    channel, monkeypatch, synthetic_environment, batch,
):
    repo, phone = synthetic_environment
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    original = cli.device_for

    def lost_during_device_construction(*args):
        os.close(channel[1])
        return original(*args)

    monkeypatch.setattr(cli, "device_for", lost_during_device_construction)
    command = (
        ["batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC A",
         "--keyword", "SYNTHETIC B"] if batch else
        ["collect-current", "--device", "synthetic"]
    )
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 3, result.stdout
    assert phone.mock_calls == []
    if batch:
        row = BatchRepository(repo).status()["batches"][0]
        assert row["status"] == "paused" and row["stop_reason"] == "operator_pause"
        assert len(row["tasks"]) == 2
        assert all(task["status"] == "pending" for task in row["tasks"])
    else:
        task = repo.status()["tasks"][0]
        assert task["status"] == "paused" and task["stop_reason"] == "operator_pause"
    assert handlers == {number: signal.getsignal(number) for number in handlers}


@pytest.mark.parametrize("batch", [False, True])
def test_controller_lost_during_action_pauses_at_next_boundary_and_keeps_batch_pending(
    channel, synthetic_environment, batch,
):
    repo, phone = synthetic_environment
    phone.start_app.side_effect = lambda _: os.close(channel[1])
    command = (
        ["batch", "run", "--device", "synthetic", "--keyword", "SYNTHETIC A",
         "--keyword", "SYNTHETIC B"] if batch else
        ["run", "--device", "synthetic", "--keyword", "SYNTHETIC A"]
    )
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 3, result.stdout
    phone.start_app.assert_called_once()
    phone.capture.assert_not_called()
    tasks = repo.status()["tasks"]
    assert len(tasks) == (2 if batch else 1)
    assert all(task["observation_count"] == 0 for task in tasks)
    assert sum(task["status"] == "paused" for task in tasks) == 1
    assert sum(task["status"] == "pending" for task in tasks) == int(batch)
    if batch:
        assert BatchRepository(repo).status()["batches"][0]["status"] == "paused"


def test_real_subprocess_exits_when_controller_pipe_is_lost(channel):
    program = """
from xhs_mobile.control import desktop_controlled, execution_stop
@desktop_controlled
def synthetic():
    event, stopped = execution_stop()
    print('SYNTHETIC ready', flush=True)
    assert event.wait(5), 'controller loss was not observed'
    assert stopped()
    print('SYNTHETIC paused', flush=True)
synthetic()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, pass_fds=(channel[0],),
    )
    try:
        assert child.stdout.readline().strip() == "SYNTHETIC ready"
        os.close(channel[1])
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert "SYNTHETIC paused" in stdout
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
