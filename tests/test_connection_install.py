"""SYNTHETIC relay migration tests. Every launchctl call uses a recording fake."""

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_mobile.connection import atomic_private_json
from xhs_mobile.domain import DeviceBusy
from xhs_mobile.locking import DeviceLock


@pytest.fixture
def install(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/install_connection_relay.py"
    spec = importlib.util.spec_from_file_location("_SYNTHETIC_relay_install", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project, home = tmp_path / "SYNTHETIC-project", tmp_path / "SYNTHETIC-home"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts/adb_interface_relay.py").write_text("# SYNTHETIC no executable code\n")
    (project / "config.local.toml").write_text('state_dir = "var"\n')
    monkeypatch.setattr(module.Path, "home", lambda: home)
    service = "SYNTHETIC.mobile-lab01-adb-relay"
    record = project / "var/connection/mobile-lab01.json"
    route = record.with_name("mobile-lab01-route.json")
    atomic_private_json(record, {"adb": {
        "launchd_service": service, "remote_host": "192.0.2.10", "remote_port": 12345,
        "local_serial": "127.0.0.1:6100", "outbound_interface": "en7",
    }})
    arguments = [sys.executable, str(project / "scripts/adb_interface_relay.py"),
                 "--interface", "en7", "--remote-host", "192.0.2.10",
                 "--remote-port", "12345", "--listen-host", "127.0.0.1",
                 "--listen-port", "6100", "--connect-timeout", "10"]
    document = {"Label": service, "ProgramArguments": arguments,
                "KeepAlive": True, "StandardErrorPath": str(project / "SYNTHETIC.log")}
    plist = home / "Library/LaunchAgents" / f"{service}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps(document))
    fake = SimpleNamespace(module=module, project=project, home=home, service=service,
                           record=record, route=route, plist=plist, arguments=list(arguments),
                           loaded=list(arguments), calls=[], bootstrap_failures=0,
                           bootout_failure=False, mismatch_once=False)

    def command(argv):
        fake.calls.append(argv)
        assert argv[0] == "/bin/launchctl", "Only mocked launchctl is allowed"
        action = argv[1]
        if action == "print":
            if fake.loaded is None:
                return subprocess.CompletedProcess(argv, 113, b"", b"SYNTHETIC not loaded")
            args = fake.loaded
            if fake.mismatch_once and "--route-config" in args:
                fake.mismatch_once = False
                args = ["SYNTHETIC-unexpected-loaded-arguments"]
            text = "service = {\n\targuments = {\n"
            text += "\n".join(f"\t\t{arg}" for arg in args) + "\n\t}\n}\n"
            return subprocess.CompletedProcess(argv, 0, text.encode(), b"")
        if action == "bootout":
            if fake.bootout_failure:
                return subprocess.CompletedProcess(argv, 1, b"", b"SYNTHETIC bootout failure")
            fake.loaded = None
        elif action == "bootstrap":
            if fake.bootstrap_failures:
                fake.bootstrap_failures -= 1
                return subprocess.CompletedProcess(argv, 1, b"", b"SYNTHETIC bootstrap failure")
            fake.loaded = plistlib.loads(Path(argv[-1]).read_bytes())["ProgramArguments"]
        else:
            pytest.fail(f"Unexpected command {action}")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(module, "_command", command)
    return fake


def test_installer_seeds_route_preserves_args_and_verifies_loaded_service(install):
    old = install.plist.read_bytes()
    result = install.module.upgrade_relay(install.project)
    assert result["changed"] is True and result["loaded"] is True
    assert install.loaded == install.arguments + ["--route-config", str(install.route)]
    assert json.loads(install.route.read_text()) == {"interface": "en7"}
    assert install.route.stat().st_mode & 0o777 == 0o600
    backup = Path(result["backup_dir"])
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "relay.plist").read_bytes() == old
    assert (backup / "relay.plist").stat().st_mode & 0o777 == 0o600
    assert [call[1] for call in install.calls] == ["print", "bootout", "bootstrap", "print"]
    document = plistlib.loads(install.plist.read_bytes())
    assert document["KeepAlive"] is True
    assert document["StandardErrorPath"].endswith("SYNTHETIC.log")
    target = f"gui/{os.getuid()}/{install.service}"
    assert install.calls[1] == ["/bin/launchctl", "bootout", target]


def test_repeated_install_does_not_restart_or_rewrite_correct_loaded_service(install):
    install.module.upgrade_relay(install.project)
    before = install.plist.stat().st_mtime_ns
    install.calls.clear()
    result = install.module.upgrade_relay(install.project)
    assert result["changed"] is False and result["backup_dir"] is None
    assert install.plist.stat().st_mtime_ns == before
    assert [call[1] for call in install.calls] == ["print"]


def test_existing_selected_route_is_preserved_and_backed_up(install):
    atomic_private_json(install.route, {"interface": "en9"})
    result = install.module.upgrade_relay(install.project)
    assert json.loads(install.route.read_text()) == {"interface": "en9"}
    assert json.loads((Path(result["backup_dir"]) / "route.json").read_text()) == {
        "interface": "en9",
    }


def test_unloaded_owned_service_is_bootstrapped_without_bootout(install):
    install.loaded = None
    assert install.module.upgrade_relay(install.project)["loaded"] is True
    assert [call[1] for call in install.calls] == ["print", "bootstrap", "print"]


def test_correct_on_disk_arguments_do_not_hide_stale_loaded_program(install):
    document = plistlib.loads(install.plist.read_bytes())
    document["ProgramArguments"] += ["--route-config", str(install.route)]
    install.plist.write_bytes(plistlib.dumps(document))
    result = install.module.upgrade_relay(install.project)
    assert result["changed"] is True
    assert install.loaded == document["ProgramArguments"]
    assert [call[1] for call in install.calls] == ["print", "bootout", "bootstrap", "print"]


@pytest.mark.parametrize("failure", ["bootstrap", "verification"])
def test_failed_migration_restores_exact_plist_route_and_running_arguments(install, failure):
    original = install.plist.read_bytes()
    atomic_private_json(install.route, {"interface": "en9"})
    original_route = install.route.read_bytes()
    if failure == "bootstrap":
        install.bootstrap_failures = 1
    else:
        install.mismatch_once = True
    with pytest.raises(install.module.RelayInstallError, match="已恢复原有配置"):
        install.module.upgrade_relay(install.project)
    assert install.plist.read_bytes() == original
    assert install.route.read_bytes() == original_route
    assert install.loaded == install.arguments


def test_failure_after_seeding_removes_new_route_when_rolling_back(install):
    install.bootstrap_failures = 1
    with pytest.raises(install.module.RelayInstallError, match="已恢复"):
        install.module.upgrade_relay(install.project)
    assert not install.route.exists()
    assert install.loaded == install.arguments


def test_failed_bootout_does_not_stop_or_restart_original_service_twice(install):
    before = install.plist.read_bytes()
    install.bootout_failure = True
    with pytest.raises(install.module.RelayInstallError, match="已恢复"):
        install.module.upgrade_relay(install.project)
    assert install.plist.read_bytes() == before
    assert install.loaded == install.arguments
    assert [call[1] for call in install.calls] == ["print", "bootout", "print"]


def test_failed_migration_restores_prior_unloaded_state(install):
    install.loaded = None
    install.bootstrap_failures = 1
    with pytest.raises(install.module.RelayInstallError, match="已恢复"):
        install.module.upgrade_relay(install.project)
    assert install.loaded is None
    assert not install.route.exists()


def test_failed_rollback_reports_retained_backup_for_manual_repair(install):
    install.bootstrap_failures = 2
    with pytest.raises(install.module.RelayInstallError, match="回退需要检查；备份"):
        install.module.upgrade_relay(install.project)
    assert list((install.project / "var/connection/relay-backups").glob("*/relay.plist"))


@pytest.mark.parametrize("existing_route", [True, False])
def test_rollback_status_timeout_restores_original_files(install, monkeypatch, existing_route):
    original = install.plist.read_bytes()
    original_route = None
    if existing_route:
        atomic_private_json(install.route, {"interface": "en9"})
        original_route = install.route.read_bytes()
    command = install.module._command
    rollback_status_pending = False

    def fail_bootstrap_then_status(arguments):
        nonlocal rollback_status_pending
        if arguments[1] == "bootstrap" and "--route-config" in (
            plistlib.loads(install.plist.read_bytes())["ProgramArguments"]
        ):
            rollback_status_pending = True
            return subprocess.CompletedProcess(arguments, 1, b"", b"SYNTHETIC bootstrap failed")
        if arguments[1] == "print" and rollback_status_pending:
            rollback_status_pending = False
            raise subprocess.TimeoutExpired("SYNTHETIC launchctl print", 12)
        return command(arguments)

    monkeypatch.setattr(install.module, "_command", fail_bootstrap_then_status)
    with pytest.raises(install.module.RelayInstallError, match="回退需要检查；备份"):
        install.module.upgrade_relay(install.project)
    assert install.plist.read_bytes() == original
    if original_route is None:
        assert not install.route.exists()
    else:
        assert install.route.read_bytes() == original_route
    assert install.loaded == install.arguments


@pytest.mark.parametrize("mutation", [
    "different_label", "different_script", "different_python", "different_remote_host",
    "different_remote_port", "public_listener", "different_listener_port", "unknown_flag",
    "duplicate_flag", "other_route_file", "shell_program",
])
def test_service_ownership_and_fixed_endpoints_are_checked_before_any_mutation(install, mutation):
    document = plistlib.loads(install.plist.read_bytes())
    args = document["ProgramArguments"]
    if mutation == "different_label":
        document["Label"] = "SYNTHETIC-someone-else"
    elif mutation == "different_script":
        args[1] = "/SYNTHETIC/other-project/scripts/adb_interface_relay.py"
    elif mutation == "different_python":
        args[0] = "/bin/sh"
    elif mutation == "different_remote_host":
        args[args.index("--remote-host") + 1] = "192.0.2.99"
    elif mutation == "different_remote_port":
        args[args.index("--remote-port") + 1] = "9999"
    elif mutation == "public_listener":
        args[args.index("--listen-host") + 1] = "0.0.0.0"
    elif mutation == "different_listener_port":
        args[args.index("--listen-port") + 1] = "22"
    elif mutation == "unknown_flag":
        args += ["--execute", "SYNTHETIC"]
    elif mutation == "duplicate_flag":
        args += ["--remote-port", "12345"]
    elif mutation == "other_route_file":
        args += ["--route-config", "/SYNTHETIC/other-route.json"]
    elif mutation == "shell_program":
        document["Program"] = "/bin/sh"
    install.plist.write_bytes(plistlib.dumps(document))
    before = install.plist.read_bytes()
    with pytest.raises(install.module.RelayInstallError):
        install.module.upgrade_relay(install.project)
    assert install.plist.read_bytes() == before
    assert not install.calls and not install.route.exists()


def test_loaded_different_service_cannot_be_hijacked_by_replacing_plist(install):
    install.loaded = ["/SYNTHETIC/different-service"]
    with pytest.raises(install.module.RelayInstallError, match="未停止该服务"):
        install.module.upgrade_relay(install.project)
    assert [call[1] for call in install.calls] == ["print"]


def test_existing_nonprivate_route_is_not_silently_overwritten(install):
    atomic_private_json(install.route, {"interface": "en9"})
    install.route.chmod(0o644)
    with pytest.raises(install.module.RelayInstallError, match="私有文件"):
        install.module.upgrade_relay(install.project)
    assert not install.calls


def test_collector_lock_prevents_migration_before_service_inspection(install):
    with DeviceLock("127.0.0.1:6100", install.project / "var"):
        with pytest.raises(DeviceBusy):
            install.module.upgrade_relay(install.project)
    assert not install.calls
