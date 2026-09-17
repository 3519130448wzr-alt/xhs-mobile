"""SYNTHETIC connection tests: all cloud, ADB, route and network I/O are doubles."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from xhs_mobile.config import Settings
from xhs_mobile.connection import (
    ConnectionFailure,
    ConnectionManager,
    atomic_private_json,
    network_fingerprint,
    public_cidr,
)
from xhs_mobile.domain import DeviceBusy
from xhs_mobile.locking import DeviceLock


@pytest.fixture
def manager(tmp_path):
    state = tmp_path / "SYNTHETIC-state"
    record = state / "connection/mobile-lab01.json"
    atomic_private_json(record, {
        "adb": {"local_serial": "127.0.0.1:6100", "outbound_interface": "en7",
                "launchd_service": "SYNTHETIC.relay"},
        "auto_connection": {"enabled": True, "prefix_list_id": "SYNTHETIC-prefix",
                            "helper_path": "/SYNTHETIC/helper"},
    })
    result = ConnectionManager(Settings(state_dir=state), "lab01")
    result.transport_healthy = Mock(return_value=False)
    result._ensure_relay = Mock()
    result.candidate_interfaces = Mock(return_value=["system", "en7", "en9"])
    result._public_address = Mock(side_effect=lambda interface: {
        "system": "1.1.1.1/32", "en7": "9.9.9.9/32", "en9": "8.8.4.4/32",
    }[interface])
    result._wait_transport = Mock(return_value=True)
    result.entries = ["8.8.8.8/32"]
    result.actions = []

    def helper(operation, cidr=None):
        result.actions.append((operation, cidr))
        if operation == "add_candidate" and cidr not in result.entries:
            result.entries.append(cidr)
        if operation == "remove_candidate" and cidr in result.entries:
            result.entries.remove(cidr)
        return {"ok": True, "entries": list(result.entries), "max_entries": 2, "associated": True}

    result._helper = Mock(side_effect=helper)
    return result


@pytest.mark.parametrize("value", [
    "0.0.0.0/0", "10.0.0.1/32", "127.0.0.1/32", "192.0.2.1/32", "8.8.8.0/24",
    "8.8.8.1/24", "::1/128", "8.8.8.8;echo x", "224.0.0.1/32", "239.1.2.3/32",
    "192.0.0.9/32", "192.88.99.1/32", {}, None, 134744072,
])
def test_only_single_public_ipv4_authorizations(value):
    with pytest.raises(ConnectionFailure, match="公网"):
        public_cidr(value)


def test_healthy_transport_is_reused_without_cloud_or_relay_mutation(manager):
    manager.transport_healthy.return_value = True
    consume = Mock()
    assert manager.ensure_connected(consume_attempt=consume) == "127.0.0.1:6100"
    manager._helper.assert_not_called()
    manager._ensure_relay.assert_not_called()
    manager._wait_transport.assert_not_called()
    consume.assert_not_called()


def test_success_checks_adb_before_removing_old_and_finishes_journal(manager):
    def connected():
        assert manager.entries == ["8.8.8.8/32", "1.1.1.1/32"]
        journal = json.loads(manager.journal_path.read_text())
        assert journal["phase"] == "adding"
        assert journal["previous_route"] == "en7"
        return True

    manager._wait_transport.side_effect = connected
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert manager.entries == ["1.1.1.1/32"]
    assert json.loads(manager.journal_path.read_text())["phase"] == "idle"
    assert json.loads(manager.route_path.read_text()) == {"interface": "system"}
    assert manager.journal_path.stat().st_mode & 0o777 == 0o600


def test_system_failure_rolls_back_before_physical_route_and_charges_each(manager):
    manager._wait_transport.side_effect = [False, True]
    consume = Mock(return_value=True)
    assert manager.ensure_connected(consume_attempt=consume) == "127.0.0.1:6100"
    assert consume.call_count == 2
    assert manager.entries == ["9.9.9.9/32"]
    assert manager.actions.index(("remove_candidate", "1.1.1.1/32")) < (
        manager.actions.index(("add_candidate", "9.9.9.9/32"))
    )
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}


def test_budget_exhaustion_stops_before_network_or_cloud_mutation(manager):
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected(consume_attempt=lambda: False)
    assert error.value.code == "connection_recovery_exhausted"
    manager._public_address.assert_not_called()
    manager._helper.assert_not_called()


def test_max_three_candidates_and_rollback_preserves_original_route(manager):
    manager._wait_transport.return_value = False
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "offline"
    assert manager._wait_transport.call_count == 3
    assert manager.entries == ["8.8.8.8/32"]
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}


def test_api_failure_never_replays_add_when_readback_confirms_success(manager):
    original = manager._helper.side_effect

    def uncertain(operation, cidr=None):
        value = original(operation, cidr)
        if operation == "add_candidate":
            raise ConnectionFailure("cloud_timeout", "SYNTHETIC timeout after commit")
        return value

    manager._helper.side_effect = uncertain
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert len([item for item in manager.actions if item[0] == "add_candidate"]) == 1


@pytest.mark.parametrize("phase,remaining", [
    ("adding", ["8.8.8.8/32"]), ("rolling_back", ["8.8.8.8/32"]),
    ("verified", ["1.1.1.1/32"]),
])
def test_restart_reconciles_update_before_health_fastpath(manager, phase, remaining):
    manager.entries.append("1.1.1.1/32")
    manager._write_journal({
        "version": 1, "phase": phase, "prefix_list_id": "SYNTHETIC-prefix",
        "candidate": "1.1.1.1/32", "old_entries": ["8.8.8.8/32"],
        "interface": "system", "previous_route": "en7",
    })
    atomic_private_json(manager.route_path, {"interface": "system"})
    manager.transport_healthy.return_value = True
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert manager.entries == remaining
    expected = "system" if phase == "verified" else "en7"
    assert json.loads(manager.route_path.read_text()) == {"interface": expected}
    manager._wait_transport.assert_not_called()


def test_cancellation_preserves_recoverable_journal_and_restores_previous_route(manager):
    def cancel_after_authorization():
        manager.stop_requested = lambda: True
        manager._check()

    manager._wait_transport.side_effect = cancel_after_authorization
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "cancelled"
    assert manager.entries == ["8.8.8.8/32", "1.1.1.1/32"]
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}
    assert json.loads(manager.journal_path.read_text())["phase"] == "adding"
    manager.stop_requested = lambda: False
    manager.transport_healthy.return_value = True
    manager.ensure_connected()
    assert manager.entries == ["8.8.8.8/32"]


def test_concurrent_cloud_change_is_never_deleted(manager):
    manager._write_journal({
        "version": 1, "phase": "adding", "prefix_list_id": "SYNTHETIC-prefix",
        "candidate": "1.1.1.1/32", "old_entries": ["8.8.8.8/32"], "previous_route": "en7",
    })
    manager.entries = ["8.8.8.8/32", "9.9.9.9/32"]
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "journal_pending"
    assert not any(operation.startswith("remove") for operation, _ in manager.actions)


def test_bad_journal_types_fail_as_explanatory_error(manager):
    manager._write_journal({
        "version": 1, "phase": "adding", "prefix_list_id": "SYNTHETIC-prefix",
        "candidate": "1.1.1.1/32", "old_entries": [{}], "previous_route": "en7",
    })
    with pytest.raises(ConnectionFailure):
        manager.ensure_connected()
    manager._helper.assert_not_called()


def test_delayed_list_and_adb_propagation_are_polled_without_second_add(manager):
    original = manager._helper.side_effect
    observed = {"hidden": False}

    def eventual(operation, cidr=None):
        value = original(operation, cidr)
        if operation == "add_candidate":
            observed["hidden"] = True
        elif operation == "inspect" and observed["hidden"]:
            observed["hidden"] = False
            value["entries"] = ["8.8.8.8/32"]
        return value

    manager._helper.side_effect = eventual
    manager._sleep = Mock()
    manager._wait_transport = ConnectionManager._wait_transport.__get__(manager)
    manager._connect_transport = Mock(side_effect=[False, True])
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert manager._connect_transport.call_count == 2
    assert len([item for item in manager.actions if item[0] == "add_candidate"]) == 1


def test_transport_requires_explicit_serial_state_and_readonly_shell(manager):
    manager.transport_healthy = ConnectionManager.transport_healthy.__get__(manager)
    manager._command = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, b"device\n", b""),
        subprocess.CompletedProcess([], 0, b"xhs-link-ok\n", b""),
    ])
    assert manager.transport_healthy()
    calls = manager._command.call_args_list
    assert calls[0].args[0] == ["adb", "-s", "127.0.0.1:6100", "get-state"]
    assert calls[1].args[0] == ["adb", "-s", "127.0.0.1:6100", "shell", "echo", "xhs-link-ok"]
    manager._helper.assert_not_called()


def test_tcp_accept_or_device_state_without_shell_cannot_pass(manager):
    manager.transport_healthy = ConnectionManager.transport_healthy.__get__(manager)
    manager._command = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, b"device\n", b""),
        subprocess.TimeoutExpired("SYNTHETIC-shell", 4),
    ])
    assert manager.transport_healthy() is False


def test_unauthorized_adb_is_actionable_without_any_cloud_calls(manager):
    manager.transport_healthy = ConnectionManager.transport_healthy.__get__(manager)
    manager._command = Mock(return_value=subprocess.CompletedProcess(
        [], 1, b"", b"error: device unauthorized\n",
    ))
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "authorization"
    manager._helper.assert_not_called()


def test_command_cancellation_kills_pending_child_without_secret_output(manager):
    manager.stop_requested = lambda: time.monotonic() - started > 0.15
    started = time.monotonic()
    with pytest.raises(ConnectionFailure) as error:
        manager._command([sys.executable, "-c", "import time; time.sleep(60)"], 10)
    assert error.value.code == "cancelled"
    assert time.monotonic() - started < 2


def test_shared_deadline_preempts_long_subprocess_timeout(manager):
    started = time.monotonic()
    manager.deadline = started + 0.2
    with pytest.raises((ConnectionFailure, subprocess.TimeoutExpired)):
        manager._command([sys.executable, "-c", "import time; time.sleep(60)"], 30)
    assert time.monotonic() - started < 2


def test_round_deadline_is_120_seconds_and_timeout_preserves_recovery_journal(manager):
    def expire_round():
        remaining = manager.deadline - time.monotonic()
        assert 118 < remaining <= 120
        manager.deadline = time.monotonic() - 0.01
        manager._check()

    manager._wait_transport.side_effect = expire_round
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "connection_timeout"
    assert json.loads(manager.journal_path.read_text())["phase"] == "adding"
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}
    assert manager.deadline is None


def test_transport_timeout_restores_route_even_when_cloud_rollback_cannot_run(manager):
    original = manager._helper.side_effect
    timed_out = False

    def fail_transport():
        nonlocal timed_out
        timed_out = True
        raise subprocess.TimeoutExpired("SYNTHETIC-adb", 9)

    def fail_cloud_after_transport(operation, cidr=None):
        if timed_out and operation == "inspect":
            raise ConnectionFailure("cloud_timeout", "SYNTHETIC unavailable during rollback")
        return original(operation, cidr)

    manager._wait_transport.side_effect = fail_transport
    manager._helper.side_effect = fail_cloud_after_transport
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "cloud_timeout"
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}
    assert json.loads(manager.journal_path.read_text())["phase"] == "adding"
    assert manager.entries == ["8.8.8.8/32", "1.1.1.1/32"]


def test_native_helper_protocol_contains_no_secrets_and_sanitizes_failure(manager):
    manager._helper = ConnectionManager._helper.__get__(manager)
    manager._command = Mock(return_value=subprocess.CompletedProcess(
        [], 1, json.dumps({"ok": False, "code": "credentials",
                           "message": "SYNTHETIC-secret-never-display"}).encode(),
        b"SYNTHETIC-secret-never-display",
    ))
    with pytest.raises(ConnectionFailure) as error:
        manager._helper("add_candidate", "1.1.1.1/32")
    assert error.value.code == "credentials"
    assert "SYNTHETIC-secret" not in str(error.value)
    call = manager._command.call_args
    assert call.args[0] == ["/SYNTHETIC/helper", "--connection-helper", "--record",
                            str(manager.record_path)]
    assert set(json.loads(call.kwargs["input_bytes"])) == {"operation", "cidr", "timeout_seconds"}


def test_physical_interfaces_follow_service_order_and_need_active_ipv4(manager):
    manager.candidate_interfaces = ConnectionManager.candidate_interfaces.__get__(manager)
    order = b"(Hardware Port: SYNTHETIC, Device: en9)\n(Hardware Port: TEST, Device: en3)\n"
    order += b"(Hardware Port: TEST, Device: en7)\n(Hardware Port: TEST, Device: utun5)\n"
    manager._command = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, order, b""),
        subprocess.CompletedProcess([], 0, b"inet 192.0.2.10\nstatus: active", b""),
        subprocess.CompletedProcess([], 0, b"status: inactive", b""),
        subprocess.CompletedProcess([], 0, b"inet 192.0.2.11\nstatus: active", b""),
    ])
    assert manager.candidate_interfaces() == ["system", "en9", "en7"]


@pytest.mark.parametrize("interface", ["system", "en7"])
def test_ip_probe_uses_same_explicit_interface_and_bypasses_http_proxy(manager, interface):
    manager._public_address = ConnectionManager._public_address.__get__(manager)
    manager._command = Mock(return_value=subprocess.CompletedProcess([], 0, b"1.1.1.1\n", b""))
    assert manager._public_address(interface) == "1.1.1.1/32"
    arguments = manager._command.call_args.args[0]
    assert arguments[arguments.index("--noproxy") + 1] == "*"
    if interface != "system":
        assert arguments[arguments.index("--interface") + 1] == interface
    else:
        assert "--interface" not in arguments


def test_network_fingerprint_does_not_touch_device_or_public_network():
    command = Mock(return_value=subprocess.CompletedProcess([], 0, b"SYNTHETIC-network", b""))
    assert network_fingerprint(command) == "SYNTHETIC-network"
    assert command.call_args.args[0] == ["/usr/sbin/scutil", "--nwi"]


def test_disabled_or_missing_configuration_does_not_break_legacy_chinese_device(tmp_path):
    manager = ConnectionManager(Settings(state_dir=tmp_path), "实验手机")
    assert manager.auto_enabled is False
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "configuration"


def test_missing_authorization_mode_preserves_prefix_list_default(manager):
    assert manager.authorization_mode == "prefix_list"


@pytest.mark.parametrize("mode", ["unknown", "", None, {}, []])
def test_unknown_authorization_mode_fails_before_connecting(manager, mode):
    manager.auto["authorization_mode"] = mode
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == "configuration"
    manager.transport_healthy.assert_not_called()
    manager._public_address.assert_not_called()
    manager._helper.assert_not_called()


@pytest.fixture
def open_manager(manager):
    manager.auto.clear()
    manager.auto.update({"enabled": True, "authorization_mode": "open"})
    # Corrupt legacy cloud transaction data must not matter in open mode.
    manager.journal_path.write_text("SYNTHETIC invalid prefix journal")
    manager._public_address.side_effect = AssertionError("public IP probe in open mode")
    manager._helper.side_effect = AssertionError("cloud/Keychain helper in open mode")
    return manager


def test_open_mode_without_cloud_configuration_ignores_old_journal(open_manager):
    manager = open_manager
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert manager.journal_path.read_text() == "SYNTHETIC invalid prefix journal"
    assert json.loads(manager.route_path.read_text()) == {"interface": "system"}
    assert json.loads(manager.open_route_journal_path.read_text())["phase"] == "idle"
    manager._public_address.assert_not_called()
    manager._helper.assert_not_called()


def test_open_healthy_connection_avoids_all_route_and_external_work(open_manager):
    manager = open_manager
    manager.transport_healthy.return_value = True
    consume = Mock()
    assert manager.ensure_connected(consume_attempt=consume) == "127.0.0.1:6100"
    manager._public_address.assert_not_called()
    manager._helper.assert_not_called()
    manager._ensure_relay.assert_not_called()
    manager._wait_transport.assert_not_called()
    manager.candidate_interfaces.assert_not_called()
    consume.assert_not_called()
    assert not manager.open_route_journal_path.exists()


def test_open_fallback_restores_before_next_route_and_keeps_attempt_budget(open_manager):
    manager = open_manager
    manager._wait_transport.side_effect = [False, True]
    originals = []
    original = manager._try_open_route

    def attempt(interface):
        originals.append(manager._previous_route())
        original(interface)

    manager._try_open_route = attempt
    consume = Mock(return_value=True)
    assert manager.ensure_connected(consume_attempt=consume) == "127.0.0.1:6100"
    assert originals == ["en7", "en7"]
    assert consume.call_count == 2
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}


def test_open_failed_candidates_are_limited_to_three_and_restore_route(open_manager):
    manager = open_manager
    manager._wait_transport.return_value = False
    manager.candidate_interfaces.return_value = ["system", "en9", "en3", "en7"]
    consume = Mock(return_value=True)
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected(consume_attempt=consume)
    assert error.value.code == "offline"
    assert consume.call_count == manager._wait_transport.call_count == 3
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}
    assert json.loads(manager.open_route_journal_path.read_text())["phase"] == "idle"


@pytest.mark.parametrize("reason", ["cancelled", "connection_timeout", "authorization", "io"])
def test_open_cancel_timeout_or_failure_restores_route_without_cloud(open_manager, reason):
    manager = open_manager

    def fail():
        assert json.loads(manager.route_path.read_text()) == {"interface": "system"}
        assert json.loads(manager.open_route_journal_path.read_text())["phase"] == "switching"
        if reason == "cancelled":
            manager.stop_requested = lambda: True
        elif reason == "connection_timeout":
            assert 118 < manager.deadline - time.monotonic() <= 120
            manager.deadline = time.monotonic() - 1
        elif reason == "authorization":
            raise ConnectionFailure("authorization", "SYNTHETIC unauthorized ADB")
        else:
            raise OSError("SYNTHETIC I/O failure")
        manager._check()

    manager._wait_transport.side_effect = fail
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected()
    assert error.value.code == ("configuration" if reason == "io" else reason)
    assert manager._wait_transport.call_count == 1
    assert json.loads(manager.route_path.read_text()) == {"interface": "en7"}
    assert json.loads(manager.open_route_journal_path.read_text())["phase"] == "idle"
    assert manager.deadline is None


@pytest.mark.parametrize("phase,expected", [("switching", "en7"), ("verified", "en9")])
def test_open_restart_recovers_only_local_route_journal(open_manager, phase, expected):
    manager = open_manager
    atomic_private_json(manager.route_path, {"interface": "en9"})
    atomic_private_json(manager.open_route_journal_path, {
        "version": 1, "phase": phase, "interface": "en9", "previous_route": "en7",
    })
    manager.transport_healthy.return_value = True
    assert manager.ensure_connected() == "127.0.0.1:6100"
    assert json.loads(manager.route_path.read_text()) == {"interface": expected}
    assert json.loads(manager.open_route_journal_path.read_text())["phase"] == "idle"
    assert manager.journal_path.read_text() == "SYNTHETIC invalid prefix journal"
    manager._wait_transport.assert_not_called()


def test_open_budget_exhaustion_never_changes_route(open_manager):
    manager = open_manager
    with pytest.raises(ConnectionFailure) as error:
        manager.ensure_connected(consume_attempt=lambda: False)
    assert error.value.code == "connection_recovery_exhausted"
    assert not manager.route_path.exists()
    assert not manager.open_route_journal_path.exists()
    manager._wait_transport.assert_not_called()


def test_open_connection_respects_device_lock_before_recovery(open_manager):
    manager = open_manager
    with DeviceLock(manager.serial, manager.settings.state_dir), pytest.raises(DeviceBusy):
        manager.ensure_connected()
    manager.transport_healthy.assert_not_called()
    manager._wait_transport.assert_not_called()
    manager._helper.assert_not_called()
    assert not manager.route_path.exists()
