"""SYNTHETIC desktop service tests. No live phone, relay, database or App is used."""

import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from xhs_mobile.batches import BatchRepository
from xhs_mobile.config import DeviceConfig, Policy, Settings
from xhs_mobile.desktop import DesktopController
from xhs_mobile.desktop_runtime import DesktopError
from xhs_mobile.desktop_store import DesktopStore
from xhs_mobile.domain import utcnow
from xhs_mobile.models import Base, Batch, Observation, Run, Task
from xhs_mobile.repository import Repository, identifier


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic.sqlite'}")
    Base.metadata.create_all(engine)
    repository = Repository(engine)
    yield repository
    engine.dispose()


class SyntheticRuntime:
    def __init__(self, repo, tmp_path):
        self.repo = repo
        self.settings = Settings(state_dir=tmp_path)
        self.device_config = DeviceConfig(serial_env="SYNTHETIC_SERIAL", session_ref="synthetic")
        self.device_id = "lab01"
        self.log_dir = tmp_path / "synthetic-logs"
        self.log_dir.mkdir()
        self.cancelled = threading.Event()
        self.release = threading.Event()
        self.started = threading.Event()
        self.hold = False
        self.calls = []
        self.fail_observe = False

    def log_exception(self, error):
        self.calls.append(("logged", type(error).__name__))

    def database(self):
        self.calls.append(("database",))
        self.check_update("database", "ready", "SYNTHETIC 数据库")

    def connect(self):
        self.calls.append(("connect",))
        if self.fail_observe:
            raise DesktopError("offline", "SYNTHETIC 断开")
        self.check_update("connection", "ready", "SYNTHETIC 仅连接")

    def auto_connection_enabled(self):
        return True

    def device(self):
        self.calls.append(("device",))

    def ensure_not_cancelled(self):
        if self.cancelled.is_set():
            raise DesktopError("cancelled", "SYNTHETIC 已取消")

    def pause(self):
        self.cancelled.set()

    def cli(self, *arguments, collector=False):
        self.calls.append(("cli", arguments, collector))
        if arguments[:2] == ("batch", "run"):
            keywords = [argument.removeprefix("--keyword=") for argument in arguments
                        if argument.startswith("--keyword=")]
            limit = int(arguments[arguments.index("--limit") + 1])
            bid = BatchRepository(self.repo).create_batch(
                device_id="lab01", serial="SYNTHETIC:1", session_ref="synthetic",
                keywords=keywords, target=limit,
                policy=self.settings.new_task_policy(limit).model_dump(),
            )
            self.progress({"event": "batch_created", "batch_id": bid})
            self.started.set()
            if self.hold:
                assert self.release.wait(5), "synthetic collector release timed out"
            with self.repo.sessions.begin() as session:
                session.get(Batch, bid).status = "paused" if self.cancelled.is_set() else (
                    "collected_awaiting_review"
                )
            return 3 if self.cancelled.is_set() else 0, []
        raise AssertionError(arguments)


@pytest.fixture
def controller(repo, tmp_path):
    runtime = SyntheticRuntime(repo, tmp_path)
    events = []
    app = DesktopController(runtime, events.append)
    app.store = DesktopStore(repo, tmp_path, "lab01")
    app.events = events
    yield app
    runtime.release.set()
    app.close()


def request(controller, method, params=None, rid=None):
    return controller.request({"id": rid or identifier(), "method": method, "params": params or {}})


def joined(controller):
    controller.worker.join(5)
    assert not controller.worker.is_alive()


def task(repo, *, keyword="SYNTHETIC 中文", mode="search"):
    return repo.create_task(
        device_id="lab01", serial="SYNTHETIC:1", session_ref="synthetic", keyword=keyword,
        target=10, policy=Policy().model_dump(), mode=mode,
    )


def observation(repo, task_id, identity=None, raw="SYNTHETIC 不删除的原文"):
    run_id = identifier()
    with repo.sessions.begin() as session:
        session.add(Run(id=run_id, task_id=task_id, profile_hash="synthetic"))
        session.flush()
        row = Observation(
            id=identifier(), task_id=task_id, run_id=run_id, device_id="lab01",
            session_ref="synthetic", fingerprint="synthetic", eligible=True,
            data={"fields": {"title": {"raw": raw, "status": "present"},
                             "body": {"raw": raw, "status": "present"},
                             "author": {"raw": "SYNTHETIC 作者", "status": "present"}},
                  "body_complete": True, "warnings": []},
            evidence=[], captured_at=utcnow(), review_identity=identity,
            review_verdict="accept" if identity else None,
        )
        session.add(row)
        saved_task = session.get(Task, task_id)
        saved_task.observation_count += 1
        saved_task.eligible_count += 1
        return row.id


def test_initialize_prepares_transport_without_starting_or_diagnosing_app(controller):
    response = request(controller, "initialize")
    assert response["ok"]
    joined(controller)
    assert controller.runtime.calls == [("database",), ("connect",)]
    assert controller.snapshot()["initialized"]
    assert controller.snapshot()["tasks"] == []
    assert all(c["status"] == "unknown" for c in controller.snapshot()["device"]["checks"]
               if c["id"] in {"automation", "app"})


def test_offline_popup_has_stable_incident_and_cloud_url(controller):
    controller.runtime.fail_observe = True
    request(controller, "initialize")
    joined(controller)
    state = controller.snapshot()
    assert state["issue"]["cloud_action"]
    assert state["device"]["status"] == "offline"
    assert state["device"]["console_url"].startswith("https://wya.wuying.aliyun.com/")
    controller._issue("offline", "SYNTHETIC 断开")
    assert controller.snapshot()["issue"]["id"] == state["issue"]["id"]


def test_status_and_detail_never_probe_device_or_create_exports(controller):
    saved = task(controller.runtime.repo)
    observation(controller.runtime.repo, saved.id)
    controller.refresh()
    for _ in range(3):
        assert request(controller, "status")["ok"]
    result = request(controller, "detail", {"kind": "task", "id": saved.id})["result"]
    assert result["records"][0]["body"] == "SYNTHETIC 不删除的原文"
    assert controller.runtime.calls == []
    assert not (controller.runtime.settings.state_dir / "exports").exists()


def test_idle_network_change_connects_once_without_doctor_resume_or_task(controller):
    controller.state["initialized"] = True
    response = request(controller, "network_changed")
    assert response["result"]["accepted"]
    joined(controller)
    assert controller.runtime.calls == [("connect",)]
    assert not request(controller, "network_changed")["result"]["accepted"]
    assert controller.snapshot()["tasks"] == []


def test_network_change_while_collector_active_never_competes(controller):
    controller.runtime.hold = True
    request(controller, "start", {"mode": "keywords", "keywords": ["SYNTHETIC"], "limit": 1})
    assert controller.runtime.started.wait(3)
    before = list(controller.runtime.calls)
    assert not request(controller, "network_changed")["result"]["accepted"]
    assert controller.runtime.calls == before
    controller.runtime.release.set()
    joined(controller)


def test_network_change_with_disabled_configuration_never_connects(controller, monkeypatch):
    controller.state["initialized"] = True
    monkeypatch.setattr(controller.runtime, "auto_connection_enabled", lambda: False)
    assert request(controller, "network_changed")["result"]["accepted"]
    joined(controller)
    assert controller.runtime.calls == []
    assert controller.snapshot()["tasks"] == []
    assert not controller.snapshot()["busy"]


def test_slow_network_configuration_read_keeps_status_and_pause_responsive(
    controller, monkeypatch,
):
    controller.state["initialized"] = True
    entered, release, acknowledged = threading.Event(), threading.Event(), threading.Event()
    result = {}

    def delayed_configuration():
        result["read_thread"] = threading.current_thread().name
        entered.set()
        assert release.wait(5), "SYNTHETIC configuration read was not released"
        return True

    def request_network_check():
        result["response"] = request(controller, "network_changed")
        acknowledged.set()

    monkeypatch.setattr(controller.runtime, "auto_connection_enabled", delayed_configuration)
    bridge = threading.Thread(target=request_network_check, name="synthetic-bridge-request")
    bridge.start()
    try:
        assert entered.wait(2)
        assert acknowledged.wait(2), "Network request blocked on the configuration read"
        assert result["response"]["result"]["accepted"]
        assert result["read_thread"] == "desktop-operation"
        status = request(controller, "status")
        assert status["ok"] and status["result"]["busy"]
        assert request(controller, "pause")["ok"]
        assert controller.runtime.cancelled.is_set()
        assert controller.runtime.calls == []
        assert not request(controller, "network_changed")["result"]["accepted"]
    finally:
        release.set()
        bridge.join(2)
        joined(controller)
    # Completing the filesystem read must not reconnect after the operator paused.
    assert controller.runtime.calls == []
    assert controller.snapshot()["tasks"] == []
    assert not controller.snapshot()["busy"]
    assert controller.snapshot()["issue"] is None


def test_connection_authorization_failure_clears_spinner_and_deduplicates(controller):
    controller._check_update("connection", "checking", "SYNTHETIC 更新授权")
    controller._issue("credentials", "SYNTHETIC 请设置凭据")
    incident = controller.snapshot()["issue"]["id"]
    controller._check_update("connection", "checking", "SYNTHETIC 更新授权")
    controller._issue("credentials", "SYNTHETIC 请设置凭据")
    assert controller.snapshot()["device"]["status"] == "attention"
    assert controller.snapshot()["issue"]["id"] == incident
    controller._check_update("connection", "ready", "SYNTHETIC 已连接")
    assert controller.snapshot()["issue"] is None


@pytest.mark.parametrize("finish", ["ready", "failed", "cancelled"])
def test_finished_connection_does_not_leave_old_deadline_for_next_check(controller, finish):
    event = {"event": "connection_progress", "phase": "connecting",
             "deadline_at": "2026-09-15T08:00:00Z", "message": "SYNTHETIC connecting"}
    controller._progress(event)
    assert controller.snapshot()["device"]["connection_deadline"] == event["deadline_at"]
    if finish == "ready":
        controller._progress({**event, "phase": "ready"})
    elif finish == "failed":
        controller._check_update("connection", "attention", "SYNTHETIC failed")
    else:
        controller._finish_checks(("cancelled", "SYNTHETIC cancelled"))
    controller._check_update("database", "checking", "SYNTHETIC next check")
    assert controller.snapshot()["device"]["connection_deadline"] is None


def test_exhausted_connection_budget_shows_results_without_resume(controller):
    saved = task(controller.runtime.repo)
    with controller.runtime.repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.status = "paused"
        row.stop_reason = "connection:connection_recovery_exhausted"
    item = controller.store.item("task", saved.id)
    assert not item["can_resume"]
    controller._result_issue(item, 3, [])
    assert controller.snapshot()["issue"]["code"] == "connection_recovery_exhausted"


def test_keywords_unified_batch_dedup_and_request_replay(controller):
    params = {"mode": "keywords", "keywords": ["SYNTHETIC 中文", " SYNTHETIC 中文 ", "第二个"],
              "limit": 10}
    response = request(controller, "start", params, rid="same-request")
    assert response["result"]["keywords"] == ["SYNTHETIC 中文", "第二个"]
    joined(controller)
    assert request(controller, "start", params, rid="same-request") == response
    with controller.runtime.repo.sessions() as session:
        assert len(list(session.scalars(select(Batch)))) == 1
    items = controller.snapshot()["tasks"]
    assert len(items) == 1 and items[0]["kind"] == "batch"
    assert items[0]["export_path"]
    folder = controller.runtime.settings.state_dir / "exports/batches" / items[0]["id"]
    assert len(list(folder.iterdir())) == 1
    conflict = request(controller, "check", rid="same-request")
    assert conflict["error"]["code"] == "request_conflict"


def test_active_worker_rejects_double_start_and_quit_waits_for_real_exit(controller):
    controller.runtime.hold = True
    params = {"mode": "keywords", "keywords": ["SYNTHETIC"], "limit": 1}
    assert request(controller, "start", params)["ok"]
    assert controller.runtime.started.wait(3)
    assert request(controller, "start", params)["error"]["code"] == "busy"
    assert request(controller, "shutdown")["ok"]
    state = controller.snapshot()
    assert state["shutting_down"] and state["busy"]
    assert state["activity"]["phase"] == "pausing"
    assert controller.worker.is_alive()
    with controller.runtime.repo.sessions() as session:
        assert list(session.scalars(select(Batch)))[0].pause_requested
    assert request(controller, "cancel_shutdown")["ok"]
    assert not controller.snapshot()["shutting_down"]
    assert controller.runtime.cancelled.is_set()  # Cancelling exit must never resume.
    controller.runtime.release.set()
    joined(controller)
    assert not controller.snapshot()["busy"]


def test_shutdown_skips_autoexport_and_preserves_task(controller):
    controller.runtime.hold = True
    request(controller, "start", {"mode": "keywords", "keywords": ["SYNTHETIC"], "limit": 1})
    assert controller.runtime.started.wait(3)
    request(controller, "shutdown")
    controller.runtime.release.set()
    joined(controller)
    assert controller.snapshot()["tasks"][0]["export_path"] is None
    assert not (controller.runtime.settings.state_dir / "exports").exists()


def test_invalid_parameters_have_no_side_effects(controller):
    for keywords, limit in (([], 1), (["SYNTHETIC"] * 1, True), (["SYNTHETIC"], 501),
                            ([str(i) for i in range(21)], 1)):
        assert not request(controller, "start", {
            "mode": "keywords", "keywords": keywords, "limit": limit,
        })["ok"]
    assert controller.runtime.calls == []


def test_batch_identity_union_child_filter_and_stale_running(controller):
    repo = controller.runtime.repo
    batches = BatchRepository(repo)
    bid = batches.create_batch(device_id="lab01", serial="SYNTHETIC:1", session_ref="synthetic",
                               keywords=["A", "B"], target=10, policy=Policy().model_dump())
    ids = batches.task_ids(bid)
    for tid in ids:
        observation(repo, tid, "manual:SYNTHETIC same note")
    current = task(repo, keyword="(current detail)", mode="current")
    with repo.sessions.begin() as session:
        session.get(Task, current.id).status = "running"
    items = controller.store.history()
    assert len(items) == 2
    batch = next(item for item in items if item["kind"] == "batch")
    assert batch["observations"] == 2 and batch["confirmed_identities"] == 1
    assert len(batch["tasks"]) == 2
    old = next(item for item in items if item["kind"] == "task")
    assert old["title"] == "当前笔记采集"
    assert old["status"] == "running" and not old["active"] and old["can_resume"]


def test_cooldown_and_manual_state_survive_reads_and_require_explicit_ack(controller):
    repo = controller.runtime.repo
    saved = task(repo)
    until = utcnow() + timedelta(minutes=30)
    repo.block(["platform:xiaohongshu"], reason="SYNTHETIC rate limit", until=until)
    before = repo.status()["policy_states"]
    assert controller.store.item("task", saved.id)["cooldown_until"] == until.isoformat()
    controller.refresh()
    assert repo.status()["policy_states"] == before
    repo.block(["session:synthetic"], reason="SYNTHETIC verification")
    response = request(controller, "resume", {"kind": "task", "id": saved.id})
    assert response["error"]["code"] == "manual"
    assert controller.runtime.calls == []


def test_export_offline_preserves_jsonl_csv_and_preview_pagination(controller):
    saved = task(controller.runtime.repo)
    for raw in ("SYNTHETIC 的与和", "SYNTHETIC 不好用", "SYNTHETIC 好用"):
        observation(controller.runtime.repo, saved.id, raw=raw)
    result = controller.store.detail("task", saved.id, limit=2)
    assert result["total"] == 3 and result["next_offset"] == 2
    assert len(controller.store.detail("task", saved.id, offset=2)["records"]) == 1
    assert request(controller, "export", {"kind": "task", "id": saved.id})["ok"]
    joined(controller)
    folder = controller.store.item("task", saved.id)["export_path"]
    from pathlib import Path
    rows = [json.loads(line) for line in (Path(folder) / "notes.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    assert rows[1]["data"]["fields"]["body"]["raw"] == "SYNTHETIC 不好用"
    assert controller.runtime.calls == []


def test_export_failure_is_retriable_without_recollection(controller, monkeypatch):
    saved = task(controller.runtime.repo)
    def fail(*args):
        raise OSError("SYNTHETIC disk error")
    monkeypatch.setattr("xhs_mobile.desktop.export_bundle", fail)
    request(controller, "export", {"kind": "task", "id": saved.id})
    joined(controller)
    assert controller.snapshot()["issue"]["code"] == "export"
    assert controller.snapshot()["issue"]["can_retry"]
    assert not controller.snapshot()["issue"]["cloud_action"]
    assert all(call[0] != "cli" for call in controller.runtime.calls)


def test_export_quit_cancel_and_new_start_cannot_race_old_worker(controller, monkeypatch):
    saved = task(controller.runtime.repo)
    entered, release = threading.Event(), threading.Event()
    def exporting(*args):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr("xhs_mobile.desktop.export_bundle", exporting)
    request(controller, "export", {"kind": "task", "id": saved.id})
    assert entered.wait(2)
    request(controller, "shutdown")
    assert not controller.snapshot()["busy"]
    request(controller, "cancel_shutdown")
    assert controller.snapshot()["busy"]
    assert controller.snapshot()["activity"]["phase"] == "exporting"
    assert request(controller, "start", {"mode": "current"})["error"]["code"] == "busy"
    release.set()
    joined(controller)


def test_parent_gone_before_creation_is_a_normal_pause(controller, monkeypatch):
    def stopped(*args, **kwargs):
        controller.runtime.cancelled.set()
        return 2, [{"error": "DesktopParentGone"}]
    monkeypatch.setattr(controller.runtime, "cli", stopped)
    request(controller, "start", {"mode": "current"})
    joined(controller)
    assert controller.snapshot()["issue"] is None
    assert controller.snapshot()["tasks"] == []


def test_cloud_console_rejects_credentials_and_non_https():
    for url in ("http://example.com", "https://user:secret@example.com/", "file:///tmp/a", "https://x/\n"):
        with pytest.raises(ValueError):
            DeviceConfig(serial_env="SYNTHETIC", session_ref="synthetic", cloud_console_url=url)


def test_failure_evidence_available_without_collected_records(controller):
    repo = controller.runtime.repo
    saved = task(repo)
    root = controller.runtime.settings.state_dir / "evidence/synthetic-evidence"
    root.mkdir(parents=True)
    (root / "ui.xml").write_text("<synthetic/>")
    (root / "screen.png").write_bytes(b"SYNTHETIC not-a-real-screen")
    repo.event(saved.id, None, "failure_evidence", {"evidence": {
        "files": {"xml": {"path": "synthetic-evidence/ui.xml"},
                  "png": {"path": "synthetic-evidence/screen.png"}},
    }})
    result = controller.store.detail("task", saved.id)
    assert result["records"] == []
    assert len(result["failure_evidence_paths"]) == 2
    assert all("synthetic-evidence" in path["path"] for path in result["failure_evidence_paths"])


def test_evidence_preview_rejects_paths_outside_evidence_root(controller, tmp_path):
    secret = tmp_path / "synthetic-secret.txt"
    secret.write_text("SYNTHETIC private")
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence/escape").symlink_to(secret)
    files = [
        {"files": {"xml": {"path": "../synthetic-secret.txt"}}},
        {"files": {"xml": {"path": str(secret)}}},
        {"files": {"xml": {"path": "escape"}}},
    ]
    assert controller.store.evidence_paths(files) == []


def test_quality_uses_friendly_chinese_without_changing_raw_database_or_export(controller):
    import copy

    repo = controller.runtime.repo
    saved = task(repo)
    oid = observation(repo, saved.id, raw="SYNTHETIC 的不与好用")
    with repo.sessions.begin() as session:
        row = session.get(Observation, oid)
        data = copy.deepcopy(row.data)
        data["fields"].update({
            "published_at": {"raw": None, "status": "not_readable",
                             "reason": "selector_missing_or_empty"},
            "likes": {"raw": "1.2万", "status": "present", "normalized": 12000,
                      "approximate": True},
            "favorites": {"raw": None, "status": "not_readable", "reason": "field_not_calibrated"},
            "note_url": {"raw": None, "status": "not_readable",
                         "reason": "selector_missing_or_empty"},
            "unknown_field": {"raw": "SYNTHETIC", "status": "unknown_status",
                              "reason": "unrecognized_reason"},
        })
        data["body_complete"] = False
        data["warnings"] = ["body_not_confirmed_complete", "body_not_fully_visible",
                            "invalid_explicit_note_id", "not_image_text_detail:unknown",
                            "unrecognized_warning"]
        row.data = data
    before = repo.observations(saved.id)
    record = controller.store.detail("task", saved.id)["records"][0]
    text = record["quality"]
    assert record["page_time"] is None and record["time_status"] == "not_readable"
    assert "点赞数：已读取，近似值" in text
    assert "收藏数：无法读取（尚未适配此字段）" in text
    assert "笔记链接：无法读取" in text
    assert "正文完整性未自动判断，请人工核对" in text
    assert "正文未确认完整" not in text and "正文未全部显示" not in text
    assert "笔记标识未通过校验" in text
    assert "页面不是图文笔记详情" in text and "需人工核对" in text
    for code in ("published_at", "selector_missing_or_empty", "body_not_confirmed_complete",
                 "unknown_field", "unknown_status", "unrecognized_reason", "unrecognized_warning"):
        assert code not in text
        assert any(code in diagnostic for diagnostic in record["quality_diagnostics"])
    assert record["body"] == before[0]["data"]["fields"]["body"]["raw"]
    assert repo.observations(saved.id) == before
    assert controller.store.records("task", saved.id) == before


def checks_by_id(controller):
    return {item["id"]: item for item in controller.snapshot()["device"]["checks"]}


def assert_checks_finished(controller):
    state = controller.snapshot()
    assert not state["busy"] and state["activity"] is None
    assert state["device"]["status"] != "checking"
    assert all(item["status"] != "checking" for item in state["device"]["checks"])


def test_same_offline_failure_after_retry_deduplicates_popup_but_finishes_checks(
    controller, monkeypatch,
):
    def disconnected():
        controller.runtime.check_update("connection", "checking", "SYNTHETIC 正在连接")
        raise DesktopError("offline", "SYNTHETIC 相同断连")
    monkeypatch.setattr(controller.runtime, "device", disconnected)
    for attempt in range(2):
        assert request(controller, "check")["ok"]
        joined(controller)
        assert_checks_finished(controller)
        state = controller.snapshot()
        assert state["device"]["status"] == "offline"
        assert checks_by_id(controller)["connection"]["status"] == "offline"
        if attempt == 0:
            incident = state["issue"]["id"]
        else:
            assert state["issue"]["id"] == incident
    assert not any(call[0] == "cli" for call in controller.runtime.calls)


@pytest.mark.parametrize("code,expected", [
    ("configuration", "unknown"), ("timeout", "attention"), ("execution", "attention"),
])
def test_unfinished_connection_check_is_not_mistaken_for_offline(controller, monkeypatch,
                                                               code, expected):
    def incomplete():
        controller.runtime.check_update("connection", "checking", "SYNTHETIC 正在连接")
        raise DesktopError(code, "SYNTHETIC 检查未完成")
    monkeypatch.setattr(controller.runtime, "device", incomplete)
    assert request(controller, "check")["ok"]
    joined(controller)
    assert_checks_finished(controller)
    assert controller.snapshot()["device"]["status"] == expected
    assert checks_by_id(controller)["connection"]["status"] == expected
    assert controller.snapshot()["issue"]["code"] == code


def test_app_version_failure_finishes_automation_probe_and_preserves_connection(
    controller, monkeypatch,
):
    def mismatch():
        controller.runtime.check_update("connection", "checking", "SYNTHETIC 正在连接")
        controller.runtime.check_update("connection", "ready", "SYNTHETIC 已连接")
        controller.runtime.check_update("automation", "checking", "SYNTHETIC 检查自动化")
        raise DesktopError("app_version", "SYNTHETIC App 版本待适配")
    monkeypatch.setattr(controller.runtime, "device", mismatch)
    for _ in range(2):
        assert request(controller, "check")["ok"]
        joined(controller)
        assert_checks_finished(controller)
        assert controller.snapshot()["device"]["status"] == "attention"
        assert checks_by_id(controller)["connection"]["status"] == "ready"
        assert checks_by_id(controller)["automation"]["status"] == "unknown"
        assert checks_by_id(controller)["app"]["status"] == "attention"
        assert controller.snapshot()["issue"]["code"] == "app_version"


def test_cancelled_connection_probe_finishes_without_fake_offline_issue(controller, monkeypatch):
    entered = threading.Event()
    def waiting():
        controller.runtime.check_update("connection", "checking", "SYNTHETIC 正在连接")
        entered.set()
        assert controller.runtime.cancelled.wait(3)
        controller.runtime.ensure_not_cancelled()
    monkeypatch.setattr(controller.runtime, "device", waiting)
    assert request(controller, "check")["ok"]
    assert entered.wait(2)
    assert request(controller, "pause")["ok"]
    joined(controller)
    assert_checks_finished(controller)
    assert controller.snapshot()["device"]["status"] == "unknown"
    assert "取消" in checks_by_id(controller)["connection"]["message"]
    assert controller.snapshot()["issue"] is None


def test_cancelled_later_probe_keeps_confirmed_connection_and_clears_old_offline(
    controller, monkeypatch,
):
    controller._issue("offline", "SYNTHETIC 上次未连接")
    entered = threading.Event()
    def waiting():
        controller.runtime.check_update("connection", "ready", "SYNTHETIC 已连接")
        controller.runtime.check_update("automation", "checking", "SYNTHETIC 检查自动化")
        entered.set()
        assert controller.runtime.cancelled.wait(3)
        controller.runtime.ensure_not_cancelled()
    monkeypatch.setattr(controller.runtime, "device", waiting)
    assert request(controller, "check")["ok"]
    assert entered.wait(2)
    assert request(controller, "pause")["ok"]
    joined(controller)
    assert_checks_finished(controller)
    assert controller.snapshot()["device"]["status"] == "ready"
    assert checks_by_id(controller)["connection"]["status"] == "ready"
    assert checks_by_id(controller)["automation"]["status"] == "unknown"
    assert controller.snapshot()["issue"] is None


def test_successful_recheck_clears_recoverable_issue_but_never_manual_policy(
    controller, monkeypatch,
):
    def ready():
        for key in ("connection", "automation", "app"):
            controller.runtime.check_update(key, "ready", "SYNTHETIC 检查通过")
    monkeypatch.setattr(controller.runtime, "device", ready)
    for code in ("offline", "automation", "app_version", "timeout", "manual"):
        controller._issue(code, f"SYNTHETIC {code}")
        assert request(controller, "check")["ok"]
        joined(controller)
        assert_checks_finished(controller)
        assert controller.snapshot()["device"]["status"] == "ready"
        if code == "manual":
            assert controller.snapshot()["issue"]["code"] == "manual"
        else:
            assert controller.snapshot()["issue"] is None


@pytest.mark.parametrize('limit', [100, 101, 500])
def test_desktop_large_targets_persist_finite_policy(controller, limit):
    result = request(controller, 'start', {
        'mode': 'keywords', 'keywords': ['SYNTHETIC 500 boundary'], 'limit': limit,
    })
    assert result['ok']
    joined(controller)
    with controller.runtime.repo.sessions() as session:
        rows = list(session.scalars(select(Task)))
        assert len(rows) == 1
        assert rows[0].target == limit
        assert rows[0].policy['max_detail_visits'] == max(100, limit * 3)
        assert rows[0].policy['max_list_swipes'] == max(50, (limit * 3 + 1) // 2)


def exhaust_capture(repo, saved):
    with repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.status = 'paused'
        row.stop_reason = 'DeviceError: capture: persistent retry budget exhausted'
        row.retry_counts = {'capture': 3, 'connection:attempt': 2}
        row.detail_visits = 7
        row.list_swipes = 2


def test_old_capture_limit_does_not_block_resume_or_change_connection(controller, monkeypatch):
    repo = controller.runtime.repo
    saved = task(repo)
    exhaust_capture(repo, saved)
    controller._check_update('connection', 'ready', 'SYNTHETIC 已连接')
    controller._check_update('automation', 'ready', 'SYNTHETIC 自动化正常')
    item = controller.store.item('task', saved.id)
    assert not item['requires_read_retry'] and item['can_resume']
    assert item['requires_read_check']
    assert item['read_retry']['available_attempts'] is None
    controller._result_issue(item, 3, [])
    state = controller.snapshot()
    assert state['issue']['code'] == 'read_anomaly'
    assert not state['issue']['can_retry']
    assert state['device']['status'] == 'ready'
    assert all(c['status'] == 'ready' for c in state['device']['checks'][:2])
    for _ in range(2):
        controller.refresh()
    arguments = []
    monkeypatch.setattr(controller, '_check', lambda: None)
    monkeypatch.setattr(controller, '_run', lambda args: arguments.append(args))
    accepted = request(controller, 'resume', {'kind': 'task', 'id': saved.id})
    assert accepted['ok']
    joined(controller)
    assert arguments == [['resume', '--task', saved.id]]
    assert repo.capture_retry_status(saved.id)['granted_credits'] == 0
    fresh = repo.task(saved.id)
    assert fresh.retry_counts == {'capture': 3, 'connection:attempt': 2}
    assert (fresh.target, fresh.detail_visits, fresh.list_swipes) == (10, 7, 2)


def test_legacy_read_retry_uuid_is_accepted_without_forwarding_credits(controller, monkeypatch):
    saved = task(controller.runtime.repo)
    exhaust_capture(controller.runtime.repo, saved)
    arguments = []
    monkeypatch.setattr(controller, '_check', lambda: None)
    monkeypatch.setattr(controller, '_run', lambda args: arguments.append(args))
    rid = identifier()
    params = {'kind': 'task', 'id': saved.id, 'retry_read_request_id': rid}
    first = request(controller, 'resume', params, rid='SYNTHETIC same resume')
    assert first['ok']
    joined(controller)
    assert request(controller, 'resume', params, rid='SYNTHETIC same resume') == first
    assert arguments == [['resume', '--task', saved.id]]
    assert controller.runtime.repo.capture_retry_status(saved.id)['granted_credits'] == 0
    assert not request(controller, 'resume', {**params, 'retry_read_request_id': 'invalid'})['ok']


def test_capture_streak_projection_is_read_only_without_duplicate_tasks(controller):
    repo = controller.runtime.repo
    bid = BatchRepository(repo).create_batch(
        device_id='lab01', serial='SYNTHETIC:1', session_ref='synthetic',
        keywords=['SYNTHETIC A', 'SYNTHETIC B'], target=10, policy=Policy().model_dump(),
    )
    with repo.sessions() as session:
        saved = session.scalar(select(Task).where(Task.keyword == 'SYNTHETIC A'))
    exhaust_capture(repo, saved)
    with repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.consecutive_read_failures = 10
        row.stop_reason = 'consecutive_read_failures:10'
    before = controller.store.history()
    assert len(before) == 1 and before[0]['requires_read_check']
    assert not before[0]['requires_read_retry']
    assert before[0]['tasks'][0]['read_retry']['failure_count'] == 3
    assert before[0]['tasks'][0]['read_anomaly']['consecutive_failures'] == 10
    after = controller.store.item('batch', bid)
    assert not after['requires_read_retry']
    assert after['read_retry']['available_attempts'] is None
    assert after['read_retry']['failure_count'] == 3
    assert repo.task(saved.id).consecutive_read_failures == 10


def test_generic_page_error_preserves_transport_card(controller):
    saved = task(controller.runtime.repo)
    with controller.runtime.repo.sessions.begin() as session:
        session.get(Task, saved.id).stop_reason = 'DeviceError: capture: SYNTHETIC screenshot error'
    controller._check_update('connection', 'ready', 'SYNTHETIC 已连接')
    controller._result_issue(controller.store.item('task', saved.id), 3, [])
    state = controller.snapshot()
    assert state['issue']['code'] == 'automation'
    assert state['device']['checks'][0]['status'] == 'ready'
    assert state['device']['status'] != 'offline'


@pytest.mark.parametrize('code', [-2, 1, 3])
def test_committed_operator_pause_is_not_an_execution_error(controller, code):
    saved = task(controller.runtime.repo)
    with controller.runtime.repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.status = 'paused'
        row.stop_reason = 'operator_pause'
    controller.runtime.cancelled.set()
    controller._check_update('connection', 'ready', 'SYNTHETIC 已连接')
    item = controller.store.item('task', saved.id)
    values = [{'event': 'run_started', 'task_id': saved.id}, {'tasks': [{
        'id': saved.id, 'status': 'paused', 'stop_reason': 'operator_pause',
    }]}]
    controller._result_issue(item, code, values)
    assert controller.snapshot()['issue'] is None
    assert controller.snapshot()['device']['status'] == 'ready'
    assert item['can_resume']


def test_synthetic_batch_pause_log_keeps_result_without_failure(controller, monkeypatch):
    repo = controller.runtime.repo
    bid = BatchRepository(repo).create_batch(
        device_id='lab01', serial='SYNTHETIC:1', session_ref='synthetic',
        keywords=['SYNTHETIC 暂停测试'], target=3, policy=Policy().model_dump(),
    )
    with repo.sessions.begin() as session:
        batch = session.get(Batch, bid)
        saved = session.scalar(select(Task))
        task_id = saved.id
        batch.status = saved.status = 'paused'
        batch.stop_reason = saved.stop_reason = 'operator_pause'
    observation(repo, task_id)
    fixture = Path(__file__).parent / 'fixtures/desktop_pause_synthetic.json'
    values = json.loads(fixture.read_text().replace('SYNTHETIC-batch', bid).replace(
        'SYNTHETIC-task', task_id,
    ))
    exports = []
    monkeypatch.setattr(controller.runtime, 'cli', lambda *a, **kw: (-2, values))
    monkeypatch.setattr(controller, '_export', lambda item: exports.append(item['id']))
    controller.runtime.cancelled.set()
    controller.state['activity'] = {'kind': 'batch', 'id': None, 'phase': 'running'}
    controller.state['busy'] = True
    controller._work(lambda: controller._run(['SYNTHETIC']))
    state = controller.snapshot()
    assert state['issue'] is None and not state['busy'] and state['activity'] is None
    assert len(state['tasks']) == 1 and state['tasks'][0]['id'] == bid
    assert state['tasks'][0]['status'] == 'paused' and state['tasks'][0]['can_resume']
    assert state['tasks'][0]['eligible'] == 1 and exports == [bid]


@pytest.mark.parametrize('failure', ['unrequested', 'wrong_id', 'missing_report', 'database'])
def test_pause_request_or_stale_report_cannot_hide_actual_execution_failure(controller, failure):
    saved = task(controller.runtime.repo)
    with controller.runtime.repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.status = 'paused'
        row.stop_reason = 'operator_pause'
    if failure != 'unrequested':
        controller.runtime.cancelled.set()
    values = [{'tasks': [{'id': saved.id, 'status': 'paused', 'stop_reason': 'operator_pause'}]}]
    if failure == 'wrong_id':
        values[0]['tasks'][0]['id'] = 'SYNTHETIC-unrelated'
    if failure == 'missing_report':
        values = []
    if failure == 'database':
        values.append({'error': 'database_error'})
    controller._result_issue(controller.store.item('task', saved.id), 1, values)
    expected = 'database' if failure == 'database' else 'execution'
    assert controller.snapshot()['issue']['code'] == expected


@pytest.mark.parametrize('missing_name', ['title', 'author', 'body'])
def test_readability_preview_names_only_the_missing_base_field(controller, missing_name):
    import copy

    repo = controller.runtime.repo
    saved = task(repo)
    oid = observation(repo, saved.id)
    with repo.sessions.begin() as session:
        row = session.get(Observation, oid)
        data = copy.deepcopy(row.data)
        data['fields'][missing_name] = {'raw': None, 'status': 'not_readable'}
        data['body_complete'] = False
        row.data = data
        row.eligible = False
    record = controller.store.detail('task', saved.id)['records'][0]
    label = {'title': '标题', 'author': '作者', 'body': '正文'}[missing_name]
    assert record['quality'].startswith(f'基础字段需核对：{label}；')
    assert record['missing_base_fields'] == [missing_name]
    assert '基础字段不完整' not in record['quality']
    assert '正文未全部显示' not in record['quality']
    assert record['completeness_status'] == 'not_assessed'


def test_time_topics_and_identity_preview_preserves_original_evidence(controller):
    import copy

    repo = controller.runtime.repo
    saved = task(repo)
    oid = observation(repo, saved.id, raw='SYNTHETIC 很长正文.. #普通文字')
    with repo.sessions.begin() as session:
        row = session.get(Observation, oid)
        data = copy.deepcopy(row.data)
        data.update(body_complete=None, time_kind='edited', topics_status='confirmed',
                    topics=['SYNTHETIC 平台话题', 'SYNTHETIC 平台话题'],
                    topic_sources=[{'name': 'SYNTHETIC 平台话题', 'method': 'ui',
                                    'evidence_ref': 'synthetic-detail/manifest.json'}])
        data['fields']['published_at'] = {
            'raw': '编辑于 昨天 香港', 'status': 'present',
            'evidence_ref': 'synthetic-detail/manifest.json',
        }
        row.data = data
        row.note_id = 'synthetic-note-123'
        row.canonical_url = 'https://www.xiaohongshu.com/explore/synthetic-note-123'
    before = repo.observations(saved.id)
    record = controller.store.detail('task', saved.id)['records'][0]
    assert record['quality'].startswith('基础字段可读；')
    assert record['time_label'] == '编辑时间' and record['time_status_label'] == '已读取'
    assert record['page_time'] == '编辑于 昨天 香港'
    assert record['time_evidence_ref'] == 'synthetic-detail/manifest.json'
    assert record['topics'] == ['SYNTHETIC 平台话题']
    assert record['topic_sources'] == before[0]['data']['topic_sources']
    assert record['identity_status'] == 'verified'
    assert record['note_id'] == 'synthetic-note-123'
    assert repo.observations(saved.id) == before


def test_generic_hashtag_and_empty_title_are_not_promoted_by_preview(controller):
    import copy

    repo = controller.runtime.repo
    saved = task(repo)
    oid = observation(repo, saved.id, raw='SYNTHETIC #普通文字')
    with repo.sessions.begin() as session:
        row = session.get(Observation, oid)
        data = copy.deepcopy(row.data)
        data.update(topics=['#普通文字'], topics_status='unrecognized')
        data['fields']['title'] = {'raw': None, 'status': 'not_displayed',
                                  'reason': 'explicit_title_absence_marker'}
        row.data = data
    record = controller.store.detail('task', saved.id)['records'][0]
    assert record['topics'] == [] and record['topics_status'] == 'unrecognized'
    assert record['title_status'] == 'not_displayed'
    assert record['base_fields_readable'] and record['missing_base_fields'] == []
    assert record['identity_status'] == 'unverified'


def test_old_edited_time_without_kind_is_never_labeled_first_publication(controller):
    import copy

    repo = controller.runtime.repo
    saved = task(repo)
    oid = observation(repo, saved.id)
    with repo.sessions.begin() as session:
        row = session.get(Observation, oid)
        data = copy.deepcopy(row.data)
        data['fields']['published_at'] = {'raw': '编辑于 08-06 浙江', 'status': 'present'}
        row.data = data
    record = controller.store.detail('task', saved.id)['records'][0]
    assert record['time_kind'] == 'edited' and record['time_label'] == '编辑时间'
    assert record['page_time'] == '编辑于 08-06 浙江'


def test_batch_reliable_ids_are_distinct_and_independent_of_other_counts(controller):
    repo = controller.runtime.repo
    bid = BatchRepository(repo).create_batch(
        device_id='lab01', serial='SYNTHETIC:1', session_ref='synthetic',
        keywords=['SYNTHETIC A', 'SYNTHETIC B'], target=10, policy=Policy().model_dump(),
    )
    with repo.sessions() as session:
        tasks = list(session.scalars(select(Task).order_by(Task.keyword)))
    first = observation(repo, tasks[0].id, identity='synthetic-review-1')
    second = observation(repo, tasks[1].id)
    observation(repo, tasks[1].id)
    with repo.sessions.begin() as session:
        session.get(Observation, first).note_id = 'synthetic-same-reliable-id'
        session.get(Observation, second).note_id = 'synthetic-same-reliable-id'
    result = controller.store.item('batch', bid)
    assert result['observations'] == 3 and result['eligible'] == 3
    assert result['base_fields_readable'] == 3
    assert result['reliable_identities'] == 1 and result['confirmed_identities'] == 1
    assert [child['reliable_identities'] for child in result['tasks']] == [1, 1]


@pytest.mark.parametrize('streak,reason', [
    ('consecutive_read_failures', '连续 10 次页面读取失败'),
    ('consecutive_no_progress', '连续 10 次页面操作未取得进展'),
])
def test_consecutive_anomaly_alert_is_stable_and_does_not_resume_or_touch_connection(
    controller, streak, reason,
):
    repo = controller.runtime.repo
    saved = task(repo)
    with repo.sessions.begin() as session:
        row = session.get(Task, saved.id)
        row.status = 'paused'
        row.stop_reason = f'{streak}:10'
        setattr(row, streak, 10)
    controller._check_update('connection', 'ready', 'SYNTHETIC 已连接')
    item = controller.store.item('task', saved.id)
    assert item['requires_read_check']
    controller._result_issue(item, 3, [])
    first = controller.snapshot()['issue']
    for _ in range(2):
        controller.refresh()
        controller._result_issue(item, 3, [])
    assert controller.snapshot()['issue'] == first
    assert first['code'] == 'read_anomaly' and reason in first['message']
    assert not first['can_retry']
    assert controller.snapshot()['device']['status'] == 'ready'
    assert controller.runtime.calls == []
    assert getattr(repo.task(saved.id), streak) == 10
