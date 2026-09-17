"""SYNTHETIC launcher integration tests; never contact a phone or database."""

import importlib.util
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("_synthetic_launcher", scripts / "launcher.py")
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def launcher(module, tmp_path, monkeypatch):
    (tmp_path / "config.local.toml").write_text(
        'state_dir = "var"\n'
        'profile_path = "synthetic-profile.toml"\n'
        'adb_path = "/SYNTHETIC/adb"\n'
        '[devices.lab01]\n'
        'serial_env = "SYNTHETIC_ADB_SERIAL"\n'
        'session_ref = "SYNTHETIC-session"\n'
        'app_package = "synthetic.xhs.app"\n',
        encoding="utf-8",
    )
    # Export tests must not open Finder, regardless of how pytest was launched.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    instance = module.Launcher(tmp_path)
    instance.env = {"SYNTHETIC_DATABASE": "not-a-real-database"}
    return instance


@pytest.fixture
def task():
    return {
        "id": "SYNTHETIC-task-01",
        "device_id": "lab01",
        "status": "partial",
        "keyword": "SYNTHETIC 香港城市大学",
        "observation_count": 2,
        "eligible_count": 1,
        "human_verified_unique": 0,
        "stop_reason": "current_detail_incomplete",
    }


def answers(monkeypatch, *values):
    sequence = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(sequence))


def test_multiple_pretty_json_documents_retain_final_task_and_unicode(module, task):
    started = {"event": "run_started", "task_id": task["id"]}
    finished = {"tasks": [task], "real_device_acceptance": "not_inferred"}
    output = "SYNTHETIC diagnostic line\n" + json.dumps(started, indent=2) + "\n"
    output += "SYNTHETIC non-JSON warning\n"
    output += json.dumps(finished, ensure_ascii=False, indent=2) + "\n"
    values = module.documents(output)
    assert values == [started, finished]
    assert module.task_from(values) == task
    assert module.documents("incomplete log without JSON") == []


def test_synthetic_batch_pause_multimessage_log_keeps_committed_report(module):
    fixture = Path(__file__).parent / "fixtures/desktop_pause_synthetic.json"
    values = json.loads(fixture.read_text())
    output = "\n".join(json.dumps(value, ensure_ascii=False, indent=2) for value in values)
    parsed = module.documents(output)
    assert parsed == values
    assert module.batch_from(parsed)['status'] == 'paused'
    assert module.batch_from(parsed)['stop_reason'] == 'operator_pause'


def test_terminal_display_removes_control_sequences_without_changing_chinese(module):
    value = module.display("中文\x1b[2J\r伪造状态\n\x07")
    assert "中文" in value and "伪造状态" in value
    assert all(character.isprintable() for character in value)


def test_opening_and_exiting_menu_never_prepares_or_collects(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "prepare_database", lambda: pytest.fail("database contacted"))
    monkeypatch.setattr(launcher, "collect", lambda *a, **kw: pytest.fail("phone contacted"))
    answers(monkeypatch, "0")
    assert launcher.menu() is False


def test_keyword_metacharacters_remain_one_structured_argument(launcher, monkeypatch, tmp_path):
    marker = tmp_path / "SHOULD_NOT_EXIST"
    keyword = f"香港 城市大学 '; touch {marker}; $(echo hacked) `echo hacked`"
    commands = []
    monkeypatch.setattr(launcher, "prepare_database", lambda: None)
    monkeypatch.setattr(launcher, "collect", lambda argv: commands.append(argv))
    answers(monkeypatch, "2", keyword, "10")
    assert launcher.menu() is True
    assert commands == [[
        "run", "--device", "lab01", "--keyword", keyword, "--limit", "10",
    ]]
    assert not marker.exists()


@pytest.mark.parametrize("value", ["0", "501", "-1", "１", "1; echo SYNTHETIC"])
def test_invalid_target_does_not_prepare_database_or_phone(module, launcher, monkeypatch, value):
    monkeypatch.setattr(launcher, "prepare_database", lambda: pytest.fail("database contacted"))
    monkeypatch.setattr(launcher, "collect", lambda *a, **kw: pytest.fail("phone contacted"))
    answers(monkeypatch, "2", "SYNTHETIC keyword", value)
    with pytest.raises(module.LauncherError, match="1 至 500"):
        launcher.menu()


@pytest.mark.parametrize(("value", "expected"), [("", "10"), ("100", "100"),
                                                   ("101", "101"), ("500", "500")])
def test_single_keyword_target_supports_500_with_legacy_policy_defaults(
    launcher, monkeypatch, value, expected,
):
    calls = []
    assert launcher.settings.policy.max_detail_visits == 100
    monkeypatch.setattr(launcher, "prepare_database", lambda: None)
    monkeypatch.setattr(launcher, "collect", lambda argv: calls.append(argv))
    answers(monkeypatch, "2", "SYNTHETIC keyword", value)
    assert launcher.menu() is True
    assert calls == [["run", "--device", "lab01", "--keyword", "SYNTHETIC keyword",
                      "--limit", expected]]


def test_cli_passes_unicode_as_argv_and_keeps_environment_out_of_command(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "execute", lambda argv, **kwargs: calls.append((argv, kwargs)))
    keyword = "SYNTHETIC 中文; $(echo no)"
    launcher.cli("run", "--device", "lab01", "--keyword", keyword)
    argv, options = calls[0]
    assert argv[:4] == [sys.executable, "-m", "xhs_mobile.cli", "--config"]
    assert argv[4] == str(launcher.config_path)
    assert argv[-2:] == ["--keyword", keyword]
    assert options["env"] is launcher.env
    assert "not-a-real-database" not in " ".join(argv)


def test_exit_three_exports_persisted_partial_task_in_both_formats(
    launcher, task, monkeypatch, capsys,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_device", lambda: None)

    def cli(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "run":
            return 3, [{"event": "run_started", "task_id": task["id"]}, {"tasks": [task]}]
        assert arguments[0] == "export"
        return 0, [{"count": 2, "format": arguments[4]}]

    monkeypatch.setattr(launcher, "cli", cli)
    launcher.collect(["run", "--device", "lab01", "--keyword", task["keyword"]])
    exports = calls[1:]
    assert len(exports) == 2
    assert [call[4] for call in exports] == ["jsonl", "csv"]
    assert all(call[1:3] == ("--task", task["id"]) for call in exports)
    assert Path(exports[0][-1]).parent == Path(exports[1][-1]).parent
    output = capsys.readouterr().out
    assert "部分完成" in output
    assert "待人工" in output or "人工核对" in output


def test_storage_failure_queries_checkpoint_and_exports_before_reporting_error(
    module, launcher, task, monkeypatch,
):
    calls, exported = [], []
    monkeypatch.setattr(launcher, "prepare_device", lambda: None)
    monkeypatch.setattr(launcher, "show_task", lambda value: None)
    monkeypatch.setattr(launcher, "export_task", exported.append)

    def cli(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "collect-current":
            return 2, [{"event": "run_started", "task_id": task["id"]}, {
                "ok": False, "error": "database_error",
            }]
        assert arguments == ("status", "--task", task["id"])
        return 0, [{"tasks": [task]}]

    monkeypatch.setattr(launcher, "cli", cli)
    with pytest.raises(module.LauncherError, match="未正常结束"):
        launcher.collect(["collect-current", "--device", "lab01"])
    assert calls[-1] == ("status", "--task", task["id"])
    assert exported == [task]


def test_resume_reuses_task_id_without_acknowledgement_or_new_target(
    launcher, task, monkeypatch,
):
    calls = []
    task["status"] = "needs_attention"
    monkeypatch.setattr(launcher, "prepare_database", lambda: None)
    monkeypatch.setattr(launcher, "pick_task", lambda: task)
    monkeypatch.setattr(launcher, "collect", lambda argv, **kw: calls.append((argv, kw)))
    answers(monkeypatch, "3")
    assert launcher.menu() is True
    assert calls == [(["resume", "--task", task["id"]], {"task_id": task["id"]})]


def test_offline_status_and_export_skip_all_device_preparation(launcher, task, monkeypatch):
    calls, exported = [], []
    monkeypatch.setattr(launcher, "prepare_database", lambda: None)
    monkeypatch.setattr(launcher, "prepare_device", lambda: pytest.fail("phone contacted"))
    monkeypatch.setattr(launcher, "export_task", exported.append)

    def cli(*arguments, **kwargs):
        calls.append(arguments)
        assert arguments == ("status",)
        return 0, [{"tasks": [task]}]

    monkeypatch.setattr(launcher, "cli", cli)
    answers(monkeypatch, "4", "1")
    assert launcher.menu() is True
    assert calls == [("status",)]
    assert exported == [task]


def test_task_selection_shows_latest_of_current_device_first(launcher, task, monkeypatch):
    latest = {**task, "id": "SYNTHETIC-task-latest"}
    other_device = {**task, "id": "SYNTHETIC-other-device", "device_id": "other"}
    monkeypatch.setattr(launcher, "tasks", lambda: [task, latest, other_device])
    answers(monkeypatch, "1")
    assert launcher.pick_task() == latest


def test_doctor_uses_explicit_serial_without_input_or_collection(module, launcher, monkeypatch):
    adb_calls, cli_calls = [], []
    profile = SimpleNamespace(app_package="synthetic.xhs.app", app_version="SYNTHETIC-1")
    monkeypatch.setattr(module, "load_profile", lambda path: profile)
    folder = launcher.settings.state_dir / "connection"
    folder.mkdir(parents=True)
    (folder / "mobile-lab01.json").write_text(json.dumps({
        "adb": {"local_serial": "127.0.0.1:6100"},
    }))

    def run(_manager, argv, timeout, **kwargs):
        adb_calls.append(argv)
        output = b"xhs-link-ok\n" if "shell" in argv else b"device\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    def cli(*arguments, **kwargs):
        cli_calls.append(arguments)
        return 0, [{"health": {"checks": {"app_version": "SYNTHETIC-1"}}}]

    monkeypatch.setattr(module.ConnectionManager, "_command", run)
    monkeypatch.setattr(launcher, "cli", cli)
    launcher.prepare_device()
    assert adb_calls == [
        ["/SYNTHETIC/adb", "-s", "127.0.0.1:6100", "get-state"],
        ["/SYNTHETIC/adb", "-s", "127.0.0.1:6100", "shell", "echo", "xhs-link-ok"],
    ]
    assert cli_calls == [("doctor", "--device", "lab01")]
    assert launcher.env["SYNTHETIC_ADB_SERIAL"] == "127.0.0.1:6100"


def test_busy_phone_blocks_launcher_before_touching_transport(module, launcher, monkeypatch):
    profile = SimpleNamespace(app_package="synthetic.xhs.app", app_version="SYNTHETIC-1")
    monkeypatch.setattr(module, "load_profile", lambda path: profile)
    folder = launcher.settings.state_dir / "connection"
    folder.mkdir(parents=True)
    (folder / "mobile-lab01.json").write_text(json.dumps({
        "adb": {"local_serial": "127.0.0.1:6100"},
    }))
    monkeypatch.setattr(
        module.ConnectionManager, "_command", lambda *a, **kw: pytest.fail("busy transport touched")
    )
    monkeypatch.setattr(launcher, "cli", lambda *a, **kw: pytest.fail("busy phone contacted"))
    with module.DeviceLock("127.0.0.1:6100", launcher.settings.state_dir):
        with pytest.raises(module.DeviceError, match="already controls"):
            launcher.prepare_device()


@pytest.mark.parametrize("requested_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signals_forward_only_to_direct_cli_child_and_restore_handlers(
    module, launcher, monkeypatch, requested_signal,
):
    active = {signum: object() for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    original = dict(active)
    forwarded = []
    popen_options = {}

    def install(signum, handler):
        previous = active[signum]
        active[signum] = handler
        return previous

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                active[requested_signal](requested_signal, None)
                self.returncode = 3
            return self.returncode

        def send_signal(self, signum):
            forwarded.append(signum)

    def popen(argv, **kwargs):
        popen_options.update(kwargs)
        kwargs["stdout"].write('{"tasks": []}\n')
        return Child()

    monkeypatch.setattr(module.signal, "signal", install)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        module.os, "killpg", lambda *args: pytest.fail("whole process group signaled")
    )
    code, values = launcher.execute(["SYNTHETIC-PROGRAM"])
    assert code == 3 and values == [{"tasks": []}]
    assert forwarded == [signal.SIGINT]
    assert active == original
    assert popen_options["start_new_session"] is True
    assert launcher.cancel_requested is True
    assert launcher.exit_requested is (requested_signal != signal.SIGINT)


def test_cancelled_preflight_does_not_spawn_a_crawl(launcher, monkeypatch):
    def cancel_during_preparation():
        launcher.cancel_requested = True

    monkeypatch.setattr(launcher, "prepare_device", cancel_during_preparation)
    monkeypatch.setattr(launcher, "cli", lambda *a, **kw: pytest.fail("crawl spawned after cancel"))
    launcher.collect(["collect-current", "--device", "lab01"])
