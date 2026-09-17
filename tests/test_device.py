"""Synthetic driver responses only: these tests never contact a real phone."""

import hashlib
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from xhs_mobile import device as module
from xhs_mobile.device import AndroidDevice, adb_devices, dependency_report
from xhs_mobile.domain import DeviceUnavailable


def synthetic_png(width=1080, height=1920):
    """An intentionally plain synthetic image, never a genuine App screenshot."""
    output = io.BytesIO()
    with Image.new("RGB", (width, height), (32, 64, 96)) as picture:
        picture.save(output, format="PNG")
    return output.getvalue()


PNG = synthetic_png()


def result(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_constructor_and_dependencies_never_connect(monkeypatch):
    execute = Mock(side_effect=AssertionError("Must not execute commands"))
    monkeypatch.setattr(module.subprocess, "run", execute)
    phone = AndroidDevice("synthetic-device", "org.synthetic.notes")
    assert phone.serial == "synthetic-device"
    assert dependency_report()["device_contacted"] is False
    execute.assert_not_called()


@pytest.mark.parametrize("serial", ["", " ", "phone with spaces", "-d"])
def test_requires_explicit_serial(serial):
    with pytest.raises(ValueError):
        AndroidDevice(serial, "org.synthetic.notes")


def test_package_validation_prevents_shell_tokens():
    with pytest.raises(ValueError):
        AndroidDevice("synthetic", "org.synthetic;echo bad")


def test_adb_actions_have_serial_and_timeout(monkeypatch):
    run = Mock(return_value=result())
    monkeypatch.setattr(module.subprocess, "run", run)
    phone = AndroidDevice("synthetic:5555", "org.synthetic.notes", timeout=7)
    phone.click((10, 20, 30, 60))
    assert run.call_args.args[0] == [
        "adb", "-s", "synthetic:5555", "shell", "input", "tap", "20", "40"
    ]
    assert run.call_args.kwargs["timeout"] == 7
    phone.press("enter")
    phone.start_app("org.synthetic.notes")
    phone.stop_app("org.synthetic.notes")
    for call in run.call_args_list:
        assert call.args[0][:3] == ["adb", "-s", "synthetic:5555"]
        assert call.kwargs["timeout"] == 7
        assert "shell" not in call.kwargs


def test_swipe_uses_override_resolution(monkeypatch):
    run = Mock(side_effect=[result(b"Physical size: 1080x1920\nOverride size: 720x1280"), result()])
    monkeypatch.setattr(module.subprocess, "run", run)
    AndroidDevice("synthetic", "org.synthetic.notes").swipe()
    assert run.call_args.args[0][-7:] == ["input", "swipe", "360", "960", "360", "384", "500"]


def test_input_uses_bounded_worker_and_json_not_shell(monkeypatch):
    run = Mock(return_value=result(b'{"ok":true,"result":null}'))
    monkeypatch.setattr(module.subprocess, "run", run)
    value = "合成输入测试 $(command) `quoted`"
    AndroidDevice("synthetic:5555", "org.synthetic.notes", timeout=5).input_text(value)
    assert run.call_args.args[0] == [
        sys.executable, "-m", "xhs_mobile.device", "--uiautomator-worker"
    ]
    request = json.loads(run.call_args.kwargs["input"])
    assert request["serial"] == "synthetic:5555"
    assert request["text"] == value
    assert request["adb_path"] == "adb"
    assert run.call_args.kwargs["timeout"] == 5


def test_configured_adb_path_is_forwarded_to_automation_worker(monkeypatch):
    custom_adb = "/synthetic tools/platform-tools/adb"
    monkeypatch.setenv("ADBUTILS_ADB_PATH", "/synthetic-unrelated/adb")
    run = Mock(return_value=result(b'{"ok":true,"result":{}}'))
    monkeypatch.setattr(module.subprocess, "run", run)
    phone = AndroidDevice("synthetic:5555", "org.synthetic.notes", adb_path=custom_adb)
    phone._u2("info")
    request = json.loads(run.call_args.kwargs["input"])
    assert request["adb_path"] == custom_adb
    assert request["serial"] == "synthetic:5555"
    # A different worker must not change the caller's process environment.
    assert os.environ["ADBUTILS_ADB_PATH"] == "/synthetic-unrelated/adb"


@pytest.mark.parametrize("operation", ["click", "input"])
def test_timeouts_classified_as_device_unavailable(monkeypatch, operation):
    run = Mock(side_effect=subprocess.TimeoutExpired(["synthetic"], 3))
    monkeypatch.setattr(module.subprocess, "run", run)
    phone = AndroidDevice("synthetic", "org.synthetic.notes", timeout=3)
    with pytest.raises(DeviceUnavailable, match="exceeded"):
        phone.click((0, 0, 10, 10)) if operation == "click" else phone.input_text("合成测试")


def test_keyboard_interrupt_is_not_caught(monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        AndroidDevice("synthetic", "org.synthetic.notes").press("back")


def test_no_input_child_cannot_read_desktop_messages():
    # A real local subprocess represents an input-hungry adb shell. Its parent
    # still owns every byte of the synthetic desktop control message.
    read_fd, write_fd = os.pipe()
    saved_stdin = os.dup(0)
    message = b'{"id":"synthetic-pause","method":"pause"}\n'
    try:
        os.write(write_fd, message)
        os.close(write_fd)
        os.dup2(read_fd, 0)
        result = module._execute(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"], 5
        )
        assert result.strip() == b"0"
        assert os.read(read_fd, len(message)) == message
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(read_fd)


def test_explicit_worker_input_still_reaches_subprocess():
    value = '{"synthetic":"中文输入"}'
    result = module._execute(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        5, input_text=value,
    )
    assert result == value.encode("utf-8")


def test_health_missing_adb_is_diagnostic(monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=FileNotFoundError(2, "missing")))
    report = AndroidDevice("synthetic", "org.synthetic.notes").health()
    assert report["ok"] is False
    assert "Cannot execute adb" in report["errors"][0]


def test_health_missing_app_stops_before_automation(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_adb", Mock(return_value=b"device"))
    monkeypatch.setattr(phone, "_shell", Mock(side_effect=["13", "33", ""]))
    automation = Mock(side_effect=AssertionError("No helper setup for missing app"))
    monkeypatch.setattr(phone, "_u2", automation)
    report = phone.health()
    assert report["ok"] is False
    assert report["checks"]["package_installed"] is False
    automation.assert_not_called()


def test_unconfigured_package_can_be_diagnosed_without_guessing(monkeypatch):
    phone = AndroidDevice("synthetic", "")
    monkeypatch.setattr(phone, "_adb", Mock(return_value=b"device"))
    shell = Mock(side_effect=[
        "13", "33", "package:org.synthetic.notes\npackage:org.synthetic.helper\n",
    ])
    monkeypatch.setattr(phone, "_shell", shell)
    monkeypatch.setattr(phone, "_foreground", Mock(return_value={
        "package": "org.synthetic.notes", "activity": ".SyntheticActivity"
    }))
    monkeypatch.setattr(phone, "_u2", Mock(return_value={"synthetic": True}))
    report = phone.health()
    assert report["ok"] is False
    assert report["needs_package_configuration"] is True
    assert report["errors"] == []
    assert report["package_candidates"] == ["org.synthetic.helper", "org.synthetic.notes"]
    assert report["checks"]["package_installed"] is None
    assert shell.call_args.args == ("pm", "list", "packages", "-3")
    with pytest.raises(ValueError):
        phone.start_app("")


def test_unconfigured_capture_observes_foreground_version(monkeypatch):
    phone = AndroidDevice("synthetic", "")
    monkeypatch.setattr(phone, "_u2", Mock(return_value='<hierarchy synthetic="true"/>'))
    monkeypatch.setattr(phone, "_adb", Mock(return_value=PNG))
    monkeypatch.setattr(phone, "_foreground", Mock(return_value={
        "package": "org.synthetic.notes", "activity": ".SyntheticActivity"
    }))
    version = Mock(return_value="synthetic-2")
    monkeypatch.setattr(phone, "_version", version)
    snapshot = phone.capture()
    version.assert_called_once_with("org.synthetic.notes")
    assert snapshot.metadata["app_version"] == "synthetic-2"
    assert snapshot.metadata["configured_package"] == ""


def test_capture_contains_original_bytes_and_metadata(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(return_value='<hierarchy synthetic="true"/>'))
    monkeypatch.setattr(phone, "_adb", Mock(return_value=PNG))
    monkeypatch.setattr(phone, "_foreground", Mock(return_value={
        "package": "org.synthetic.notes", "activity": ".SyntheticActivity"
    }))
    monkeypatch.setattr(phone, "_version", Mock(return_value="synthetic-1"))
    shot = phone.capture()
    assert shot.png == PNG
    assert shot.xml == '<hierarchy synthetic="true"/>'
    assert shot.metadata["resolution"] == [1080, 1920]
    assert shot.metadata["app_version"] == "synthetic-1"
    assert shot.metadata["package"] == "org.synthetic.notes"
    assert shot.metadata["source_kind"] == "android"
    assert set(shot.metadata["capture_timings"]) == {
        "hierarchy_seconds", "screenshot_transfer_seconds", "screenshot_verify_seconds",
        "metadata_seconds", "total_seconds",
    }
    assert all(value >= 0 for value in shot.metadata["capture_timings"].values())
    assert shot.captured_at.tzinfo is not None


def test_capture_keeps_readback_metadata_bound_to_original_png(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    xml = '<hierarchy synthetic="true"/>'
    readbacks = {
        "schema_version": 1, "status": "unavailable", "entries": [],
        "reasons": ["SYNTHETIC incomplete readback"],
        "xml_sha256": hashlib.sha256(xml.encode()).hexdigest(),
    }
    automation = Mock(return_value={"xml": xml, "readbacks": readbacks})
    monkeypatch.setattr(phone, "_u2", automation)
    monkeypatch.setattr(phone, "_adb", Mock(return_value=PNG))
    monkeypatch.setattr(phone, "_foreground", Mock(return_value={
        "package": "org.synthetic.notes", "activity": ".SyntheticActivity"
    }))
    monkeypatch.setattr(phone, "_version", Mock(return_value="synthetic-1"))
    shot = phone.capture()
    automation.assert_called_once_with(
        "capture_hierarchy", configured_package="org.synthetic.notes"
    )
    assert shot.xml == xml
    assert shot.metadata["ui_readbacks"]["reasons"] == ["SYNTHETIC incomplete readback"]
    assert shot.metadata["ui_readbacks"]["screenshot_sha256"] == hashlib.sha256(PNG).hexdigest()


def test_capture_recovers_plain_tree_after_readback_worker_timeout(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    xml = '<hierarchy synthetic="true"/>'
    automation = Mock(side_effect=[DeviceUnavailable("synthetic timeout"), xml])
    monkeypatch.setattr(phone, "_u2", automation)
    monkeypatch.setattr(phone, "_adb", Mock(return_value=PNG))
    monkeypatch.setattr(phone, "_foreground", Mock(return_value={
        "package": "org.synthetic.notes", "activity": ".SyntheticActivity"
    }))
    monkeypatch.setattr(phone, "_version", Mock(return_value="synthetic-1"))
    shot = phone.capture()
    assert shot.xml == xml
    assert automation.call_args_list[-1].args == ("hierarchy",)
    assert shot.metadata["ui_readbacks"]["status"] == "unavailable"
    assert shot.metadata["ui_readbacks"]["reasons"] == ["capture_worker_unavailable"]


def test_worker_capture_dispatch_preserves_explicit_serial_and_package(monkeypatch):
    from xhs_mobile import ui_readback

    fake = Mock()
    fake.settings = {}
    connect = Mock(return_value=fake)
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))
    monkeypatch.setenv("ADBUTILS_ADB_PATH", "/synthetic/old-adb")
    capture = Mock(return_value={"xml": '<hierarchy synthetic="true"/>', "readbacks": {}})
    monkeypatch.setattr(ui_readback, "capture_hierarchy", capture)
    request = {
        "serial": "synthetic-explicit", "adb_path": "/synthetic/adb", "timeout": 9,
        "operation": "capture_hierarchy", "configured_package": "org.synthetic.notes",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    module._uiautomator_worker()
    connect.assert_called_once_with("synthetic-explicit")
    capture.assert_called_once_with(fake, "org.synthetic.notes")
    fake.send_keys.assert_not_called()
    fake.click.assert_not_called()
    assert json.loads(output.getvalue())["ok"] is True


def test_capture_rejects_error_instead_of_screenshot(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(return_value="<hierarchy/>"))
    monkeypatch.setattr(phone, "_adb", Mock(return_value=b"error: device offline"))
    with pytest.raises(DeviceUnavailable, match="not PNG"):
        phone.capture()


@pytest.mark.parametrize("missing_bytes", [1, 8, len(PNG) // 2])
def test_capture_rejects_truncated_png_even_with_a_valid_header(monkeypatch, missing_bytes):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(return_value="<hierarchy synthetic='true'/>"))
    truncated = PNG[:-missing_bytes]
    assert truncated[:24] == PNG[:24]
    monkeypatch.setattr(phone, "_adb", Mock(return_value=truncated))
    with pytest.raises(DeviceUnavailable, match="truncated"):
        phone.capture()


def test_capture_rejects_corrupt_pixel_data_with_intact_header_and_end_chunk(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(return_value="<hierarchy synthetic='true'/>"))
    corrupt = bytearray(PNG)
    idat = PNG.index(b"IDAT")
    corrupt[idat + 6] ^= 0xFF
    assert corrupt[:24] == PNG[:24] and corrupt[-12:] == PNG[-12:]
    monkeypatch.setattr(phone, "_adb", Mock(return_value=bytes(corrupt)))
    with pytest.raises(DeviceUnavailable, match="integrity verification"):
        phone.capture()


def test_adb_inventory_does_not_select_a_phone(monkeypatch):
    run = Mock(return_value=result(
        b"List of devices attached\nsynthetic-one device product:test\nsynthetic-two offline\n"
    ))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert adb_devices() == [
        {"serial": "synthetic-one", "state": "device", "details": "product:test"},
        {"serial": "synthetic-two", "state": "offline", "details": ""},
    ]
    assert run.call_args.args[0] == ["adb", "devices", "-l"]


def test_worker_explicit_serial_and_chinese_input(monkeypatch):
    fake = Mock()
    fake.settings = {}
    custom_adb = "/synthetic tools/platform-tools/adb"

    def connect_with_configured_adb(serial):
        assert os.environ["ADBUTILS_ADB_PATH"] == custom_adb
        return fake

    connect = Mock(side_effect=connect_with_configured_adb)
    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(connect=connect))
    monkeypatch.setenv("ADBUTILS_ADB_PATH", "/synthetic-unrelated/adb")
    request = {
        "serial": "synthetic", "timeout": 9, "operation": "input_text", "text": "中文",
        "adb_path": custom_adb,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    module._uiautomator_worker()
    connect.assert_called_once_with("synthetic")
    fake.send_keys.assert_called_once_with("中文", clear=True)
    assert os.environ["ADBUTILS_ADB_PATH"] == request["adb_path"]
    assert fake.settings["wait_timeout"] == 9
    assert json.loads(output.getvalue()) == {"ok": True, "result": None}


def test_check_input_explicit_readback_without_submit(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    before = (
        '<hierarchy><node class="android.widget.EditText" focused="true" text=""/></hierarchy>'
    )
    after = (
        '<hierarchy><node class="android.widget.EditText" focused="true" text="中文"/></hierarchy>'
    )
    automation = Mock(side_effect=[before, None, after])
    monkeypatch.setattr(phone, "_u2", automation)
    adb = Mock(side_effect=AssertionError("Must not submit a search"))
    monkeypatch.setattr(phone, "_adb", adb)
    report = phone.check_input("中文")
    assert report == {
        "ok": True, "input_attempted": True, "submitted": False,
        "readback_prefix": "", "reason": "exact_ui_readback",
    }
    assert automation.call_args_list[1].kwargs == {"text": "中文"}


def test_check_input_refuses_password_fields(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    xml = (
        '<hierarchy><node class="android.widget.EditText" '
        'focused="true" password="true"/></hierarchy>'
    )
    monkeypatch.setattr(phone, "_u2", Mock(return_value=xml))
    input_text = Mock(side_effect=AssertionError("Must not type into password fields"))
    monkeypatch.setattr(phone, "input_text", input_text)
    assert phone.check_input()["input_attempted"] is False


def synthetic_input(text="", **attributes):
    from xml.etree.ElementTree import Element, tostring

    node = Element("node", {
        "class": "android.widget.EditText", "resource-id": "synthetic/search-input",
        "package": "org.synthetic.notes", "bounds": "[10,20][300,60]",
        "focused": "true", "text": text, **attributes,
    })
    return tostring(node, encoding="unicode")


@pytest.mark.parametrize(("prefix", "display", "expected"), [
    ("", "中文输入测试", True),
    ("搜索, ", "搜索, 中文输入测试", True),
    ("搜索, ", "其他, 中文输入测试", False),
    ("搜索, ", "搜索, 搜索, 中文输入测试", False),
    ("搜索, ", "搜索, 中文输入", False),
    ("搜索, ", " 搜索, 中文输入测试", False),
    ("搜索, ", "搜索, 中文输入测试 ", False),
    ("", "搜索, 中文输入测试", False),
])
def test_check_input_uses_exact_calibrated_display_prefix(monkeypatch, prefix, display, expected):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    automation = Mock(side_effect=[
        "<hierarchy>" + synthetic_input() + "</hierarchy>",
        None,
        "<hierarchy>" + synthetic_input(display) + "</hierarchy>",
    ])
    monkeypatch.setattr(phone, "_u2", automation)
    monkeypatch.setattr(phone, "_adb", Mock(side_effect=AssertionError("No submission permitted")))
    report = phone.check_input(readback_prefix=prefix)
    assert report["ok"] is expected
    assert report["readback_prefix"] == prefix
    assert report["input_attempted"] is True
    assert report["submitted"] is False
    assert "中文输入测试" not in json.dumps(report, ensure_ascii=False)
    assert automation.call_args_list[1].kwargs == {"text": "中文输入测试"}


@pytest.mark.parametrize("second_field", [synthetic_input(), synthetic_input(password="true")])
def test_check_input_refuses_ambiguous_focused_inputs_before_typing(monkeypatch, second_field):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    xml = "<hierarchy>" + synthetic_input() + second_field + "</hierarchy>"
    monkeypatch.setattr(phone, "_u2", Mock(return_value=xml))
    type_text = Mock(side_effect=AssertionError("Must not type into ambiguous input"))
    monkeypatch.setattr(phone, "input_text", type_text)
    report = phone.check_input()
    assert not report["ok"]
    assert not report["input_attempted"]
    type_text.assert_not_called()


@pytest.mark.parametrize("after_contents", [
    synthetic_input("中文输入测试") * 2,
    synthetic_input("中文输入测试", **{"resource-id": "synthetic/other-field"}),
    synthetic_input("中文输入测试", package="org.synthetic.other"),
    synthetic_input("中文输入测试", bounds="[10,90][300,130]"),
    "<node><node/>" + synthetic_input("中文输入测试") + "</node>",
    synthetic_input("中文输入测试", password="true"),
    synthetic_input("中文输入测试", focused="false"),
    synthetic_input("", **{"content-desc": "中文输入测试"}),
])
def test_check_input_rejects_changed_ambiguous_or_unreadable_field(monkeypatch, after_contents):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(side_effect=[
        "<hierarchy>" + synthetic_input() + "</hierarchy>",
        None,
        "<hierarchy>" + after_contents + "</hierarchy>",
    ]))
    report = phone.check_input()
    assert report["input_attempted"] is True
    assert report["ok"] is False
    assert report["reason"] == "input_not_readable_or_mismatch"


def test_check_input_accepts_reordering_top_level_windows(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    app_window = "<node><node>" + synthetic_input("搜索, 中文输入测试") + "</node></node>"
    other_window = '<node package="synthetic.ime"/>'
    monkeypatch.setattr(phone, "_u2", Mock(side_effect=[
        "<hierarchy>" + other_window + app_window + "</hierarchy>",
        None,
        "<hierarchy>" + app_window + other_window + "</hierarchy>",
    ]))
    report = phone.check_input(readback_prefix="搜索, ")
    assert report["input_attempted"] is True
    assert report["ok"] is True


def test_check_input_rejects_reordering_within_same_window(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    field = synthetic_input("搜索, 中文输入测试")
    monkeypatch.setattr(phone, "_u2", Mock(side_effect=[
        "<hierarchy><node><node/>" + field + "</node></hierarchy>",
        None,
        "<hierarchy><node>" + field + "<node/></node></hierarchy>",
    ]))
    report = phone.check_input(readback_prefix="搜索, ")
    assert report["input_attempted"] is True
    assert report["ok"] is False


@pytest.mark.parametrize("hidden_on_input", [False, True])
def test_check_input_never_types_into_hidden_fields_or_ancestors(monkeypatch, hidden_on_input):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    field = (
        synthetic_input(**{"visible-to-user": "false"}) if hidden_on_input else synthetic_input()
    )
    window = '<node>' if hidden_on_input else '<node visible-to-user="false">'
    xml = "<hierarchy>" + window + field + "</node></hierarchy>"
    monkeypatch.setattr(phone, "_u2", Mock(return_value=xml))
    type_text = Mock(side_effect=AssertionError("Must not type into a hidden input"))
    monkeypatch.setattr(phone, "input_text", type_text)
    assert phone.check_input()["input_attempted"] is False
    type_text.assert_not_called()


def test_check_input_hidden_duplicate_does_not_make_visible_field_ambiguous(monkeypatch):
    phone = AndroidDevice("synthetic", "org.synthetic.notes")
    hidden = '<node visible-to-user="false">' + synthetic_input(password="true") + '</node>'
    before = "<hierarchy><node>" + synthetic_input() + "</node>" + hidden + "</hierarchy>"
    after = (
        "<hierarchy><node>" + synthetic_input("中文输入测试") + "</node>" + hidden + "</hierarchy>"
    )
    monkeypatch.setattr(phone, "_u2", Mock(side_effect=[before, None, after]))
    assert phone.check_input()["ok"] is True
