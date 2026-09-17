"""SYNTHETIC batch menu tests; never contact a phone, real database, or shell.

The launcher uses a temporary configuration, with external boundaries replaced
by recording callbacks. Batch/child records below are deliberately synthetic.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from xhs_mobile.locking import DeviceLock


@pytest.fixture
def module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "_synthetic_launcher_batch", scripts / "launcher.py",
    )
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
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    value = module.Launcher(tmp_path)
    value.env = {"SYNTHETIC_DATABASE": "not-a-real-database"}
    monkeypatch.setattr(value, "prepare_database", lambda: pytest.fail("real DB preparation"))
    monkeypatch.setattr(value, "prepare_device", lambda: pytest.fail("real phone preparation"))
    monkeypatch.setattr(value, "cli", lambda *a, **kw: pytest.fail("external CLI execution"))
    return value


@pytest.fixture
def batch():
    return {
        "id": "SYNTHETIC-batch-01", "device_id": "lab01", "status": "paused",
        "stop_reason": "operator_pause", "tasks": [
            {
                "id": "SYNTHETIC-child-01", "device_id": "lab01",
                "keyword": "SYNTHETIC 甲", "status": "collected_awaiting_review",
                "observation_count": 12, "eligible_count": 10, "target": 10,
                "human_verified_unique": 0,
            },
            {
                "id": "SYNTHETIC-child-02", "device_id": "lab01",
                "keyword": "SYNTHETIC 乙", "status": "paused",
                "observation_count": 4, "eligible_count": 2, "target": 10,
                "human_verified_unique": 0,
            },
        ],
    }


def answers(monkeypatch, *values):
    sequence = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(sequence))


def test_multiline_keywords_preserve_unicode_metacharacters_and_option_prefixes(
    launcher, monkeypatch, tmp_path,
):
    marker = tmp_path / "SYNTHETIC_SHOULD_NOT_EXIST"
    keywords = [
        f"中文 空格 '; touch {marker}; $(echo synthetic) `echo synthetic`",
        "--limit 99999", "SYNTHETIC 香港 城市大学",
    ]
    calls = []
    monkeypatch.setattr(launcher, "prepare_database", lambda: calls.append("database"))
    monkeypatch.setattr(launcher, "collect_batch", lambda argv: calls.append(argv))
    answers(monkeypatch, "6", *keywords, "", "")

    assert launcher.menu() is True
    assert calls == ["database", [
        "batch", "run", "--device", "lab01", "--limit", "10",
        *[f"--keyword={keyword}" for keyword in keywords],
    ]]
    assert not marker.exists()


def test_cancel_batch_input_before_any_database_or_device_preparation(launcher, monkeypatch):
    answers(monkeypatch, "6", "")
    assert launcher.menu() is True


def test_duplicate_keywords_rejected_after_whitespace_normalization_before_database(
    module, launcher, monkeypatch,
):
    answers(monkeypatch, "6", "  SYNTHETIC 中文  ", "SYNTHETIC 中文")
    with pytest.raises(module.LauncherError, match="关键词重复"):
        launcher.menu()


@pytest.mark.parametrize("limit", ["0", "501", "１０", "10; echo synthetic"])
def test_invalid_batch_target_rejected_before_database(module, launcher, monkeypatch, limit):
    answers(monkeypatch, "6", "SYNTHETIC 中文", "", limit)
    with pytest.raises(module.LauncherError, match="1 至 500"):
        launcher.menu()


@pytest.mark.parametrize("limit", ["100", "101", "500"])
def test_new_batch_uses_generated_budget_instead_of_legacy_100_cap(
    launcher, monkeypatch, limit,
):
    calls = []
    assert launcher.settings.policy.max_detail_visits == 100
    monkeypatch.setattr(launcher, "prepare_database", lambda: None)
    monkeypatch.setattr(launcher, "collect_batch", lambda argv: calls.append(argv))
    answers(monkeypatch, "6", "SYNTHETIC 中文", "", limit)
    assert launcher.menu() is True
    assert calls == [["batch", "run", "--device", "lab01", "--limit", limit,
                      "--keyword=SYNTHETIC 中文"]]


def test_batch_parser_uses_latest_batch_report_and_never_mistakes_child_task(module, batch):
    older = {**batch, "status": "running", "stop_reason": None}
    child = {"tasks": [batch["tasks"][1]]}
    values = [
        {"batches": [older]}, child,
        {"event": "batch_task_finished", "task_id": "SYNTHETIC-child-02"},
        {"batches": [batch]}, child,
    ]
    text = "\n".join(json.dumps(value, ensure_ascii=False, indent=2) for value in values)
    assert module.batch_from(module.documents(text)) == batch
    assert module.batch_from([child]) is None


def test_paused_exit_three_prepares_phone_once_and_exports_one_complete_bundle(
    launcher, batch, monkeypatch,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_device", lambda: calls.append(("device",)))

    def cli(*arguments, timeout=None):
        calls.append(arguments)
        if arguments[:2] == ("batch", "run"):
            return 3, [{"tasks": [batch["tasks"][1]]}, {"batches": [batch]}]
        assert arguments[:2] == ("batch", "export")
        assert timeout == 90
        return 0, [{"ok": True}]

    monkeypatch.setattr(launcher, "cli", cli)
    arguments = ["batch", "run", "--device", "lab01", "--limit", "10", "--keyword=SYNTHETIC"]
    launcher.collect_batch(arguments)

    assert calls[:2] == [("device",), tuple(arguments)]
    assert len(calls) == 3
    exported = calls[2]
    assert exported[:6] == ("batch", "export", "--batch", batch["id"], "--format", "bundle")
    assert Path(exported[7]).is_relative_to(launcher.settings.state_dir / "exports" / "batches")
    assert not any("review" in call or "--acknowledge" in call for call in calls)


@pytest.mark.parametrize("event", ["batch_created", "batch_started"])
def test_failed_run_recovers_committed_batch_from_event_and_exports_before_reporting_error(
    module, launcher, batch, monkeypatch, event,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_device", lambda: None)

    def cli(*arguments, timeout=None):
        calls.append(arguments)
        if arguments[:2] == ("batch", "run"):
            return 2, [
                {"event": event, "batch_id": batch["id"]},
                {"tasks": [batch["tasks"][1]]},
                {"error": "SYNTHETIC transient failure"},
            ]
        if arguments[:2] == ("batch", "status"):
            assert arguments == ("batch", "status", "--batch", batch["id"])
            return 0, [{"batches": [batch]}]
        assert arguments[:2] == ("batch", "export")
        return 0, [{"ok": True}]

    monkeypatch.setattr(launcher, "cli", cli)
    with pytest.raises(module.LauncherError, match="菜单 7") as error:
        launcher.collect_batch(["batch", "run", "--device", "lab01", "--keyword=SYNTHETIC"])

    assert [call[:2] for call in calls] == [
        ("batch", "run"), ("batch", "status"), ("batch", "export"),
    ]
    assert str(launcher.log_dir) in str(error.value)
    assert not any("resume" in call for call in calls)


def test_child_task_report_alone_is_not_presented_as_a_successful_batch(
    module, launcher, batch, monkeypatch,
):
    monkeypatch.setattr(launcher, "prepare_device", lambda: None)
    monkeypatch.setattr(launcher, "cli", lambda *a, **kw: (0, [{"tasks": batch["tasks"]}]))
    monkeypatch.setattr(launcher, "export_batch", lambda *_: pytest.fail("invented batch export"))
    monkeypatch.setattr(launcher, "show_batch", lambda *_: pytest.fail("invented batch status"))
    with pytest.raises(module.LauncherError, match="批次运行未正常结束"):
        launcher.collect_batch(["batch", "run", "--keyword=SYNTHETIC"])


def test_menu_resume_passes_original_batch_id_without_acknowledgement_or_new_keywords(
    launcher, batch, monkeypatch,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_database", lambda: calls.append(("database",)))
    monkeypatch.setattr(launcher, "prepare_device", lambda: calls.append(("device",)))

    def cli(*arguments, timeout=None):
        calls.append(arguments)
        if arguments[:2] in {("batch", "status"), ("batch", "resume")}:
            return 0, [{"batches": [batch]}]
        assert arguments[:2] == ("batch", "export")
        return 0, [{"ok": True}]

    monkeypatch.setattr(launcher, "cli", cli)
    answers(monkeypatch, "7", "1")
    assert launcher.menu() is True
    assert calls[:4] == [
        ("database",), ("batch", "status"), ("device",),
        ("batch", "resume", "--batch", batch["id"]),
    ]
    assert calls[4][:6] == ("batch", "export", "--batch", batch["id"], "--format", "bundle")
    assert len(calls) == 5
    assert not any("--acknowledge" in call or "run" in call or "--limit" in call for call in calls)


def test_offline_menu_eight_exports_once_while_another_launcher_holds_the_device_lock(
    launcher, batch, monkeypatch,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_database", lambda: calls.append(("database",)))

    def cli(*arguments, timeout=None):
        calls.append(arguments)
        if arguments == ("batch", "status"):
            return 0, [{"batches": [batch]}]
        assert arguments[:6] == (
            "batch", "export", "--batch", batch["id"], "--format", "bundle",
        )
        return 0, [{"ok": True}]

    monkeypatch.setattr(launcher, "cli", cli)
    answers(monkeypatch, "8", "1")
    with DeviceLock(launcher.device_id, launcher.settings.state_dir / "launcher"):
        assert launcher.menu() is True

    assert len(calls) == 3
    assert calls[:2] == [("database",), ("batch", "status")]
    assert calls[2][5] == "bundle"


def test_cancel_during_device_preparation_never_starts_batch_or_exports(launcher, monkeypatch):
    def cancelled():
        launcher.cancel_requested = True

    monkeypatch.setattr(launcher, "prepare_device", cancelled)
    launcher.collect_batch(["batch", "run", "--keyword=SYNTHETIC"])


def test_failed_bundle_export_keeps_actionable_menu_eight_message_without_retrying_collection(
    module, launcher, batch, monkeypatch,
):
    calls = []
    monkeypatch.setattr(launcher, "prepare_device", lambda: None)

    def cli(*arguments, timeout=None):
        calls.append(arguments)
        if arguments[:2] == ("batch", "run"):
            return 0, [{"batches": [batch]}]
        assert arguments[:2] == ("batch", "export")
        return 2, [{"error": "SYNTHETIC full volume"}]

    monkeypatch.setattr(launcher, "cli", cli)
    with pytest.raises(module.LauncherError, match="菜单 8"):
        launcher.collect_batch(["batch", "run", "--keyword=SYNTHETIC"])
    assert [call[:2] for call in calls] == [("batch", "run"), ("batch", "export")]
