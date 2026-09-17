"""SYNTHETIC runtime lifecycle and one-shot observation; never contact Android."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xhs_mobile import desktop_runtime as module
from xhs_mobile.desktop_runtime import DesktopError, DesktopRuntime
from xhs_mobile.domain import DeviceBusy
from xhs_mobile.locking import DeviceLock


def documents(text):
    result = []
    while text.strip():
        try:
            value, size = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            break
        result.append(value)
        text = text.lstrip()[size:]
    return result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # Bypass Launcher construction, which intentionally owns local database paths.
    adapter = DesktopRuntime.__new__(DesktopRuntime)
    adapter.log_dir = tmp_path
    adapter.project = tmp_path
    adapter.env = dict(os.environ)
    adapter._lock = threading.RLock()
    adapter._counter = 0
    adapter.process = None
    adapter.process_is_collector = False
    adapter.control_write = None
    adapter.cancelled = threading.Event()
    adapter.parse_documents = documents
    adapter.progress = Mock()
    adapter.check_update = Mock()
    adapter.settings = SimpleNamespace(state_dir=tmp_path, adb_path="SYNTHETIC-adb")
    adapter.launcher = Mock()
    adapter.launcher.connection.return_value = ({}, "SYNTHETIC-serial")
    class SyntheticLauncherError(RuntimeError):
        pass

    monkeypatch.setitem(sys.modules, "launcher", SimpleNamespace(
        LauncherError=SyntheticLauncherError,
    ))
    return adapter


@pytest.mark.parametrize("failure", ["spawn", "log"])
def test_failed_setup_closes_both_untransferred_pipe_descriptors(runtime, monkeypatch, failure):
    original_pipe = os.pipe
    pipes = []

    def tracked_pipe():
        pair = original_pipe()
        pipes.append(pair)
        return pair

    monkeypatch.setattr(module.os, "pipe", tracked_pipe)
    spawn = Mock(side_effect=OSError("SYNTHETIC spawn failure"))
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    if failure == "log":
        runtime.log_dir /= "not-created"
    with pytest.raises(OSError):
        runtime.execute(["SYNTHETIC"], collector=True)
    assert len(pipes) == 1
    for fd in pipes[0]:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert runtime.process is None and runtime.control_write is None
    if failure == "log":
        spawn.assert_not_called()


def test_failed_progress_waits_for_real_synthetic_child_to_pause_before_clearing_active(runtime):
    marker = runtime.project / "SYNTHETIC-paused.txt"
    program = """
import json, pathlib, sys, time
from xhs_mobile.control import desktop_controlled, execution_stop
@desktop_controlled
def synthetic():
    stop, _ = execution_stop()
    print(json.dumps({'event': 'run_started', 'task_id': 'SYNTHETIC'}), flush=True)
    assert stop.wait(5), 'lost desktop must request pause'
    time.sleep(0.3)  # Finish the current synthetic operation at its safe boundary.
    pathlib.Path(sys.argv[1]).write_text('SYNTHETIC safely paused')
synthetic()
"""
    callback_failed = threading.Event()
    failures = []

    def fail_progress(_):
        callback_failed.set()
        raise OSError("SYNTHETIC failed progress delivery")

    runtime.progress = fail_progress

    def execute():
        try:
            runtime.execute([sys.executable, "-c", program, str(marker)], collector=True)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert callback_failed.wait(5)
        time.sleep(0.05)
        assert runtime.process is not None
        assert runtime.process.poll() is None
        assert runtime.process_is_collector
        assert thread.is_alive(), "callback failure must not announce safe idle before child exits"
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert marker.read_text() == "SYNTHETIC safely paused"
        assert len(failures) == 1 and isinstance(failures[0], OSError)
        assert runtime.process is None and runtime.control_write is None
        assert runtime.process_is_collector is False
    finally:
        process = runtime.process
        if process is not None and process.poll() is None:
            runtime.pause()
            process.wait(timeout=5)
        thread.join(timeout=5)


def test_collector_cannot_use_forced_timeout(runtime, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="safe pause"):
        runtime.execute(["SYNTHETIC"], collector=True, timeout=1)
    spawn.assert_not_called()


def test_repeated_pause_after_final_progress_never_interrupts_committed_synthetic_child(runtime):
    fixture = Path(__file__).parent / "fixtures/desktop_pause_synthetic.json"
    program = r'''
import json, os, pathlib, select, signal, sys, time
values = json.loads(pathlib.Path(sys.argv[1]).read_text())
for value in values[:3]:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
fd = int(os.environ['XHS_DESKTOP_CONTROL_FD'])
assert select.select([fd], [], [], 5)[0], 'SYNTHETIC pause not received'
assert os.read(fd, 1) == b'', 'SYNTHETIC expected pause via closed pipe'
# Reproduce the real CLI restoring signal handlers before final progress is consumed.
signal.signal(signal.SIGINT, lambda *_: sys.exit(99))
for value in values[3:]:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
time.sleep(0.7)
sys.exit(3)
'''
    events = []

    def progress(value):
        events.append(value)
        if value.get('event') in {'batch_task_started', 'batch_task_finished'}:
            runtime.pause()
            runtime.pause()

    runtime.progress = progress
    code, values = runtime.execute([sys.executable, '-c', program, str(fixture)], collector=True)
    assert code == 3
    assert values == json.loads(fixture.read_text())
    assert [item['event'] for item in events] == [
        'batch_created', 'batch_started', 'batch_task_started', 'batch_task_finished',
    ]
    assert values[-1]['batches'][0]['stop_reason'] == 'operator_pause'
    assert runtime.process is None and runtime.control_write is None


def test_legacy_signal_pause_fallback_is_sent_once_per_process(runtime):
    first, second = Mock(), Mock()
    runtime.process_is_collector = True
    runtime.process = first
    runtime.pause()
    runtime.pause()
    assert first.send_signal.call_count == 1
    runtime.process = second
    runtime.pause()
    runtime.pause()
    assert second.send_signal.call_count == 1


def test_noncollector_does_not_inherit_control_environment(runtime):
    runtime.env["XHS_DESKTOP_CONTROL_FD"] = "999"
    code, result = runtime.execute([
        sys.executable, "-c", "import os,json; print(json.dumps({"
        "'control_present': 'XHS_DESKTOP_CONTROL_FD' in os.environ}))",
    ], timeout=5)
    assert code == 0
    assert result == [{"control_present": False}]


@pytest.mark.parametrize("collector", [False, True])
def test_real_child_cannot_consume_desktop_requests_and_control_pause_still_works(
    tmp_path, collector,
):
    # This separate SYNTHETIC bridge process owns a real stdin pipe. Its child
    # deliberately reads stdin before acknowledging startup; inherited stdin
    # would steal the App's queued pause message before the bridge can read it.
    program = r'''
import json, os, pathlib, sys, threading
from xhs_mobile.desktop_runtime import DesktopRuntime

adapter = DesktopRuntime.__new__(DesktopRuntime)
adapter.project = adapter.log_dir = pathlib.Path(sys.argv[1])
adapter.env = dict(os.environ)
adapter._lock = threading.RLock()
adapter._counter = 0
adapter.process = adapter.control_write = None
adapter.process_is_collector = False
adapter.cancelled = threading.Event()
adapter.parse_documents = lambda text: [json.loads(line) for line in text.splitlines() if line]
ready = threading.Event()
adapter.progress = lambda value: ready.set()
collector = sys.argv[2] == 'true'
child = """
import json, os, sys
from xhs_mobile.control import desktop_controlled, execution_stop
def work():
    received = os.read(0, 65536).decode()
    print(json.dumps({'event': 'run_started', 'stdin_received': received}), flush=True)
    if sys.argv[1] == 'true':
        stop, _ = execution_stop()
        assert stop.wait(5), 'dedicated desktop control pipe did not request pause'
        print(json.dumps({'safely_paused': True}), flush=True)
if sys.argv[1] == 'true':
    desktop_controlled(work)()
else:
    work()
"""
result, failures = [], []
def run():
    try:
        result.append(adapter.execute(
            [sys.executable, '-c', child, str(collector).lower()], collector=collector,
            timeout=None if collector else 5,
        ))
    except BaseException as error:
        failures.append(type(error).__name__)
thread = threading.Thread(target=run)
thread.start()
try:
    assert ready.wait(5), 'synthetic child did not acknowledge startup'
    desktop_request = sys.stdin.readline()
finally:
    adapter.pause()
    thread.join(timeout=5)
assert not thread.is_alive(), 'child did not safely finish'
print(json.dumps({'desktop_request': desktop_request, 'result': result, 'failures': failures}))
'''
    request = json.dumps({"id": "SYNTHETIC-pause", "method": "pause"}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(collector).lower()],
        input=request, text=True, capture_output=True, timeout=15, check=True,
    )
    outcome = json.loads(completed.stdout)
    assert outcome["desktop_request"] == request
    assert outcome["failures"] == []
    code, events = outcome["result"][0]
    assert code == 0
    assert events[0]["stdin_received"] == ""
    assert any(event.get("safely_paused") for event in events) is collector


def test_runtime_command_does_not_read_its_parents_input(runtime, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"SYNTHETIC", b""))
    monkeypatch.setattr(module.subprocess, "run", run)
    runtime._command(["SYNTHETIC-read-only-command"], 5)
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_startup_observation_only_checks_explicit_serial_once(runtime):
    runtime._command = Mock(return_value=subprocess.CompletedProcess([], 0, b"device\n", b""))
    runtime.observe_connection()
    runtime._command.assert_called_once_with(["SYNTHETIC-adb", "-s", "SYNTHETIC-serial",
                                              "get-state"], 15)
    updates = runtime.check_update.call_args_list
    assert all(call.args[0] == "connection" for call in updates)
    assert updates[-1].args[1] == "ready"
    assert "尚未检查" in updates[-1].args[2]


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, b"", b"SYNTHETIC device not found"),
    subprocess.CompletedProcess([], 0, b"offline\n", b""),
    subprocess.TimeoutExpired("SYNTHETIC-adb", 15),
])
def test_startup_offline_gives_actionable_error_without_reconnection(runtime, result):
    runtime._command = Mock()
    if isinstance(result, Exception):
        runtime._command.side_effect = result
    else:
        runtime._command.return_value = result
    with pytest.raises(DesktopError) as error:
        runtime.observe_connection()
    assert error.value.code == "offline"
    runtime._command.assert_called_once()
    runtime.check_update.assert_called_with("connection", "offline", str(error.value))


def test_startup_observation_respects_physical_device_exclusion(runtime):
    runtime._command = Mock(side_effect=AssertionError("SYNTHETIC phone is busy"))
    with DeviceLock("SYNTHETIC-serial", runtime.settings.state_dir):
        with pytest.raises(DeviceBusy):
            runtime.observe_connection()
    runtime._command.assert_not_called()
    assert runtime.check_update.call_args.args[1] == "busy"


def test_cancelled_startup_does_not_observe_phone(runtime):
    runtime.cancelled.set()
    runtime._command = Mock()
    with pytest.raises(DesktopError) as error:
        runtime.observe_connection()
    assert error.value.code == "cancelled"
    runtime._command.assert_not_called()
    runtime.launcher.connection.assert_not_called()


@pytest.fixture
def configured_device(runtime, monkeypatch):
    runtime.settings.profile_path = runtime.project / "SYNTHETIC-profile.toml"
    runtime.device_id = "SYNTHETIC-device"
    runtime.device_config = SimpleNamespace(
        app_package="SYNTHETIC-app", serial_env="SYNTHETIC_SERIAL",
    )
    runtime.launcher.connection.return_value = (
        {"launchd_service": "synthetic.relay"}, "SYNTHETIC-serial",
    )
    monkeypatch.setattr(module, "load_profile", Mock(return_value=SimpleNamespace(
        app_package="SYNTHETIC-app", app_version="SYNTHETIC-version",
    )))
    runtime.cli = Mock(return_value=(0, [{"health": {"ok": True, "checks": {
        "adb_state": "device", "package_installed": True, "app_version": "SYNTHETIC-version",
    }}}]))
    return runtime


def test_device_preflight_uses_shared_manager_then_doctor(configured_device):
    runtime = configured_device
    manager = Mock()
    manager.ensure_connected.return_value = "SYNTHETIC-serial"
    runtime.connection_manager = Mock(return_value=manager)
    runtime._command = Mock(side_effect=AssertionError("duplicated connection logic"))
    runtime.device()
    manager.ensure_connected.assert_called_once_with()
    runtime._command.assert_not_called()
    runtime.cli.assert_called_once_with("doctor", "--device", "SYNTHETIC-device", timeout=180)
    assert runtime.env["SYNTHETIC_SERIAL"] == "SYNTHETIC-serial"


def test_opening_app_connects_without_doctor_or_cli(configured_device):
    runtime = configured_device
    runtime.connection_manager = Mock()
    runtime.connection_manager.return_value.ensure_connected.return_value = "SYNTHETIC-serial"
    assert runtime.connect() == "SYNTHETIC-serial"
    runtime.cli.assert_not_called()
    assert runtime.check_update.call_args.args[1] == "ready"
    assert "尚未检查" in runtime.check_update.call_args.args[2]


@pytest.fixture
def diagnosed_device(configured_device):
    runtime = configured_device
    manager = Mock()
    manager.authorization_mode = "prefix_list"
    manager.ensure_connected.return_value = "SYNTHETIC-serial"
    manager.transport_healthy.return_value = True
    runtime.connection_manager = Mock(return_value=manager)
    return runtime, manager


@pytest.mark.parametrize("health_values", [
    [],
    [{"error": "SYNTHETIC doctor failed before producing health"}],
    [{"health": None}],
    [{"health": "SYNTHETIC malformed health"}],
    [{"health": []}],
    [{"health": {}}],
    [{"health": {"checks": None}}],
    [{"health": {"checks": []}}],
    [{"health": {"checks": {"adb_state": "offline"}}}],
])
def test_incomplete_doctor_with_live_adb_is_diagnostic_not_offline(
    diagnosed_device, health_values,
):
    runtime, manager = diagnosed_device
    runtime.cli.return_value = (1, health_values)
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == "diagnostic"
    assert "手机已连接" in str(error.value)
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_called_once_with()
    runtime.cli.assert_called_once_with("doctor", "--device", "SYNTHETIC-device", timeout=180)
    updates = [call.args for call in runtime.check_update.call_args_list]
    assert updates[-1][0:2] == ("connection", "ready")
    assert not any(status == "offline" for _, status, _ in updates)


def test_incomplete_doctor_requires_failed_explicit_probe_to_report_offline(diagnosed_device):
    runtime, manager = diagnosed_device
    runtime.cli.return_value = (1, [])
    manager.transport_healthy.return_value = False
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == "offline"
    assert "连接检查失败" in str(error.value)
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_called_once_with()


@pytest.mark.parametrize("location", ["connect", "doctor", "probe"])
def test_diagnostic_device_busy_is_preserved_without_offline_or_reconnect(
    diagnosed_device, location,
):
    runtime, manager = diagnosed_device
    if location == "connect":
        manager.ensure_connected.side_effect = DeviceBusy("SYNTHETIC device locked")
    elif location == "doctor":
        runtime.cli.return_value = (1, [{"error": "DeviceBusy"}])
    else:
        runtime.cli.return_value = (1, [])
        manager.transport_healthy.side_effect = DeviceBusy("SYNTHETIC device locked")
    with pytest.raises(DeviceBusy):
        runtime.device()
    manager.ensure_connected.assert_called_once_with()
    assert manager.transport_healthy.call_count == (1 if location == "probe" else 0)
    assert runtime.cli.call_count == (0 if location == "connect" else 1)
    assert not any(call.args[1] == "offline" for call in runtime.check_update.call_args_list)


def test_diagnostic_probe_preserves_adb_authorization_failure(diagnosed_device):
    runtime, manager = diagnosed_device
    runtime.cli.return_value = (1, [])
    manager.transport_healthy.side_effect = module.ConnectionFailure(
        "authorization", "SYNTHETIC ADB needs authorization",
    )
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == "authorization"
    assert str(error.value) == "SYNTHETIC ADB needs authorization"
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_called_once_with()
    assert not any(call.args[1] == "offline" for call in runtime.check_update.call_args_list)


@pytest.mark.parametrize("code,healthy", [(1, False), (1, True), (0, False)])
def test_automation_failure_preserves_successful_connection(diagnosed_device, code, healthy):
    runtime, manager = diagnosed_device
    _, documents = runtime.cli.return_value
    documents[0]["health"]["ok"] = healthy
    runtime.cli.return_value = (code, documents)
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == "automation"
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_not_called()
    connections = [call.args for call in runtime.check_update.call_args_list
                   if call.args[0] == "connection"]
    assert connections[-1][1] == "ready"
    assert not any(call.args[1] == "offline" for call in runtime.check_update.call_args_list)


@pytest.mark.parametrize("failure", [OSError("SYNTHETIC doctor launch failure"),
                                     subprocess.TimeoutExpired("SYNTHETIC doctor", 180)])
def test_doctor_execution_failure_is_diagnostic_and_preserves_connection(
    diagnosed_device, failure,
):
    runtime, manager = diagnosed_device
    runtime.cli.side_effect = failure
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == "diagnostic"
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_not_called()
    connections = [call.args for call in runtime.check_update.call_args_list
                   if call.args[0] == "connection"]
    assert connections[-1][1] == "ready"


def test_open_connection_ready_message_does_not_claim_address_authorization(diagnosed_device):
    runtime, manager = diagnosed_device
    manager.authorization_mode = "open"
    assert runtime.connect() == "SYNTHETIC-serial"
    manager.ensure_connected.assert_called_once_with()
    manager.transport_healthy.assert_not_called()
    runtime.cli.assert_not_called()
    runtime.check_update.assert_called_once_with(
        "connection", "ready", "开放连接，无需地址授权；手机已连接",
    )


@pytest.mark.parametrize("code", ["credentials", "authorization", "offline", "connection_timeout"])
def test_shared_connection_failures_remain_actionable(configured_device, code):
    runtime = configured_device
    runtime.connection_manager = Mock()
    runtime.connection_manager.return_value.ensure_connected.side_effect = module.ConnectionFailure(
        code, "SYNTHETIC connection error",
    )
    with pytest.raises(DesktopError) as error:
        runtime.device()
    assert error.value.code == code
    assert str(error.value) == "SYNTHETIC connection error"
    runtime.cli.assert_not_called()
    assert runtime.check_update.call_args.args[1] != "checking"


def test_pause_before_connect_does_not_create_manager(configured_device):
    runtime = configured_device
    runtime.cancelled.set()
    runtime.connection_manager = Mock()
    with pytest.raises(DesktopError, match="取消"):
        runtime.connect()
    runtime.connection_manager.assert_not_called()


def test_connection_progress_preserves_deadline_and_never_starts_collector(configured_device):
    runtime = configured_device
    value = {"phase": "authorizing", "deadline_at": "2030-01-01T00:00:00Z",
             "message": "SYNTHETIC 更新授权"}
    runtime._connection_progress(value)
    runtime.progress.assert_called_once_with({"event": "connection_progress", **value})
    runtime.cli.assert_not_called()


def test_collector_connection_phases_survive_log_polling_once_each(runtime, tmp_path):
    path = tmp_path / "synthetic-progress.jsonl"
    events = [{"event": "connection_progress", "task_id": "SYNTHETIC",
               "phase": phase, "deadline_at": "2026-09-15T00:02:00Z"}
              for phase in ("recognizing", "authorizing", "connecting", "connecting", "ready")]
    seen = set()
    for count in range(1, len(events) + 1):
        path.write_text("\n".join(json.dumps(value) for value in events[:count]) + "\n")
        runtime._progress(path, seen)
        runtime._progress(path, seen)
    assert [call.args[0] for call in runtime.progress.call_args_list] == events
