"""Asynchronous desktop control API; the stdio protocol is private to the local App."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from xhs_mobile.batch_exports import export_bundle
from xhs_mobile.batches import BatchRepository
from xhs_mobile.desktop_runtime import DesktopError
from xhs_mobile.desktop_store import DesktopStore
from xhs_mobile.domain import DeviceBusy

DEFAULT_CONSOLE = "https://wya.wuying.aliyun.com/instanceLayouts"
CONNECTION_ISSUES = {
    "credentials", "authorization", "cloud_timeout", "connection_timeout", "journal_pending",
    "cloud_error", "connection_recovery_exhausted",
}
ISSUES = {
    "offline": ("云手机未连接", True),
    "automation": ("手机自动化需要检查", True),
    "diagnostic": ("手机诊断未完成", False),
    "read_anomaly": ("连续读取异常，需要人工检查", False),
    "app_version": ("小红书需要适配", True),
    "manual": ("需要在云手机上处理", True),
    "busy": ("设备正在使用", False),
    "database": ("本机数据暂不可用", False),
    "evidence": ("来源证据保存失败", False),
    "export": ("结果导出未完成", False),
    "configuration": ("本机配置需要检查", False),
    "timeout": ("准备步骤超时", False),
    "execution": ("采集已停止，请查看任务", False),
    "credentials": ("请完成自动连接授权", False),
    "authorization": ("连接授权需要检查", False),
    "cloud_timeout": ("连接授权暂时无法更新", False),
    "connection_timeout": ("本次连接已超时", True),
    "journal_pending": ("连接配置正在等待核对", False),
    "cloud_error": ("连接授权更新未完成", False),
    "connection_recovery_exhausted": ("自动重连次数已用完", True),
}


class DesktopController:
    def __init__(self, runtime, emit=lambda value: None):
        self.runtime, self.emit = runtime, emit
        self.lock = threading.RLock()
        self.store = None
        self.worker = None
        self.closed = threading.Event()
        self.responses = {}
        self.issue_sequence = 0
        self.exporting = False
        self.export_activity = None
        self.last_network_check = 0.0
        self.state = {
            "initialized": False, "busy": False, "shutting_down": False,
            "device": {
                "id": runtime.device_id, "status": "unknown",
                "message": "尚未检查云手机；打开 App 不会开始采集",
                "console_url": runtime.device_config.cloud_console_url or DEFAULT_CONSOLE,
                "checks": [
                    {"id": key, "label": label, "status": "unknown", "message": "尚未检查"}
                    for key, label in (("connection", "手机连接"), ("automation", "自动化服务"),
                                       ("app", "小红书适配"), ("database", "本机数据库"))
                ],
            },
            "activity": None, "tasks": [], "issue": None,
            "log_dir": str(runtime.log_dir),
        }
        runtime.progress = self._progress
        runtime.check_update = self._check_update
        self.monitor = threading.Thread(target=self._monitor, daemon=True, name="desktop-status")
        self.monitor.start()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def publish(self):
        self.emit({"event": "state", "data": self.snapshot()})

    def request(self, request):
        """Cache every request ID for this bridge lifetime; never replay a mutation."""
        identifier = request.get("id") if isinstance(request, dict) else None
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            return {"id": identifier, "ok": False, "error": {
                "code": "invalid_request", "message": "请求缺少有效编号。",
            }}
        fingerprint = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with self.lock:
            if identifier in self.responses:
                prior, response = self.responses[identifier]
                if prior != fingerprint:
                    return {"id": identifier, "ok": False, "error": {
                        "code": "request_conflict",
                        "message": "请求编号已用于其他操作，请刷新状态。",
                    }}
                return copy.deepcopy(response)
            # Place a guard before dispatch. Even an unexpected concurrent retry cannot start twice.
            response = {"id": identifier, "ok": False, "error": {
                "code": "request_in_progress", "message": "操作正在提交，请查看当前状态。",
            }}
            self.responses[identifier] = fingerprint, response
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("操作参数必须是对象")
            result = self._dispatch(request.get("method"), params)
            response = {"id": identifier, "ok": True, "result": result}
        except (ValueError, DesktopError, DeviceBusy) as exc:
            response = {"id": identifier, "ok": False, "error": {
                "code": getattr(exc, "code", "busy" if isinstance(exc, DeviceBusy) else "invalid"),
                "message": str(exc) if not isinstance(exc, DeviceBusy) else "设备正在使用。",
            }}
        except Exception as exc:
            self.runtime.log_exception(exc)
            response = {"id": identifier, "ok": False, "error": {
                "code": "database", "message": "读取或操作未完成，请查看诊断详情后重试。",
            }}
        with self.lock:
            self.responses[identifier] = fingerprint, response
        return copy.deepcopy(response)

    def _dispatch(self, method, params):
        if method == "status":
            return self.snapshot()
        if method == "initialize":
            if self.state["initialized"] or self.state["busy"]:
                return self.snapshot()
            self._launch(self._initialize, phase="preparing", message="正在加载本机数据")
            return {"accepted": True}
        if method == "shutdown":
            with self.lock:
                self.state["shutting_down"] = True
            self._pause_current()
            self.publish()
            return {"accepted": True}
        if method == "cancel_shutdown":
            with self.lock:
                self.state["shutting_down"] = False
                if self.exporting and self.worker and self.worker.is_alive():
                    self.state["busy"] = True
                    self.state["activity"] = copy.deepcopy(self.export_activity)
            # A pause already requested must finish. Cancelling exit never resumes collection.
            self.publish()
            return {"accepted": True}
        if method == "detail":
            return self._store().detail(
                params.get("kind"), params.get("id"), params.get("offset", 0),
                params.get("limit", 30), self.snapshot()["activity"],
            )
        if method == "pause":
            self._pause_requested(params)
            return {"accepted": True}
        if method == "check":
            self._launch(self._check, phase="preparing", message="正在检查运行条件")
            return {"accepted": True}
        if method == "network_changed":
            # Network notifications never compete with the existing worker or resume a task.
            with self.lock:
                if (self.state["busy"] or self.state["shutting_down"]
                        or not self.state["initialized"]
                        or (self.worker and self.worker.is_alive())):
                    return {"accepted": False, "reason": "busy"}
                if time.monotonic() - self.last_network_check < 3:
                    return {"accepted": False, "reason": "debounced"}
                self.last_network_check = time.monotonic()
                self._launch(self._network_check, phase="preparing",
                             message="网络已变化，正在连接手机")
            return {"accepted": True}
        if method == "start":
            mode = params.get("mode")
            if mode not in {"keywords", "current"}:
                raise ValueError("请选择关键词采集或当前笔记采集")
            keywords, limit = [], 1
            if mode == "keywords":
                values = params.get("keywords")
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError("请填写关键词")
                keywords = list(dict.fromkeys(value.strip() for value in values if value.strip()))
                if not 1 <= len(keywords) <= 20:
                    raise ValueError("请填写 1 至 20 个不同关键词")
                limit = params.get("limit", 10)
                self.runtime.settings.new_task_policy(limit)
            self._launch(
                lambda: self._collect(mode, keywords, limit), phase="preparing",
                kind="batch" if mode == "keywords" else "task", message="正在检查运行条件",
            )
            return {"accepted": True, "keywords": keywords, "limit": limit}
        if method == "resume":
            item = self._store().item(params.get("kind"), params.get("id"))
            if not item["can_resume"]:
                raise ValueError("此任务已结束，请查看或导出结果")
            acknowledge = params.get("acknowledge", False)
            if type(acknowledge) is not bool:
                raise ValueError("人工处理确认参数无效")
            if item["requires_ack"] and not acknowledge:
                raise DesktopError("manual", "请先在云手机上处理，再选择“处理后检查并继续”。")
            if item["kind"] == "task":
                self._reject_batch_child(item["id"])
            retry_request = params.get("retry_read_request_id")
            if retry_request is not None:
                if not isinstance(retry_request, str):
                    raise ValueError("读取恢复请求编号无效")
                retry_request = str(UUID(retry_request))
            # Accept the previous protocol parameter, but no longer issue or require credits.
            self._launch(
                lambda: self._collect_resume(item, acknowledge), phase="preparing",
                kind=item["kind"], identifier=item["id"], message="正在检查恢复条件",
            )
            return {"accepted": True}
        if method == "export":
            item = self._store().item(params.get("kind"), params.get("id"))
            self._launch(lambda: self._export(item), phase="exporting", kind=item["kind"],
                         identifier=item["id"], message="正在导出已保存结果")
            return {"accepted": True}
        raise ValueError("未知操作")

    def _store(self):
        if self.store is None:
            raise DesktopError("database", "本机数据尚未就绪，请先完成检查。")
        return self.store

    def _launch(self, function, *, phase, message, kind="task", identifier=None):
        with self.lock:
            if self.state["shutting_down"]:
                raise DesktopError("shutting_down", "正在安全退出，请等待或取消退出。")
            if self.state["busy"] or (self.worker and self.worker.is_alive()):
                raise DesktopError("busy", "已有操作正在运行，请查看当前任务。")
            self.runtime.cancelled.clear()
            self.state["busy"] = True
            self.state["activity"] = {
                "phase": phase, "kind": kind, "id": identifier, "keyword": "", "message": message,
            }
            self.worker = threading.Thread(target=self._work, args=(function,), daemon=True,
                                           name="desktop-operation")
            self.worker.start()
        self.publish()

    def _work(self, function):
        failure = None
        try:
            function()
        except DesktopError as exc:
            failure = (exc.code, str(exc))
            if exc.code != "cancelled":
                self._issue(exc.code, str(exc))
        except SQLAlchemyError as exc:
            self.runtime.log_exception(exc)
            failure = ("database", "本机数据操作失败，请重新检查数据库和诊断详情。")
            self._issue(*failure)
        except DeviceBusy:
            failure = ("busy", "手机正在被其他进程使用，请查看原任务，结束后再检查。")
            self._issue(*failure)
        except Exception as exc:
            self.runtime.log_exception(exc)
            failure = ("execution", "操作已停止，已提交成果保留。请查看任务与诊断详情。")
            self._issue(*failure)
        finally:
            with self.lock:
                self.state["busy"] = False
                self.state["activity"] = None
                self.state["initialized"] = True
                self._finish_checks(failure)
            self.refresh()
            self.publish()

    def _finish_checks(self, failure):
        """An operation has really ended: no check can truthfully remain in progress.

        Preserve observed ready/offline states. An unfinished probe is unknown, not
        evidence that the phone disconnected; only an actual connection failure
        supplied by the runtime sets offline.
        """
        device = self.state["device"]
        device["connection_deadline"] = None
        pending = [check for check in device["checks"] if check["status"] == "checking"]
        code = failure[0] if failure else None
        cancelled = code == "cancelled" or (not failure and self.runtime.cancelled.is_set())
        message = "检查已取消，可重新检查" if cancelled else "本次检查未完成，请重新检查"
        for check in pending:
            status = "attention" if code in {"timeout", "execution"} else "unknown"
            check.update(status=status, message=(failure[1] if status == "attention" else message))
        if not pending and device["status"] != "checking":
            return
        # _issue has already applied an observed phone/automation/app failure.
        if code in {"offline", "busy", "automation", "app_version", "manual"}:
            return
        connection = next(check for check in device["checks"] if check["id"] == "connection")
        if connection["status"] in {"offline", "busy"}:
            device.update(status=connection["status"], message=connection["message"])
        elif connection["status"] == "ready":
            device.update(status="ready", message=f"云手机已连接；{message}")
        elif connection["status"] == "attention":
            device.update(status="attention", message=connection["message"])
        else:
            device.update(status="unknown", message=message)

    def _initialize(self):
        self._database()
        self.runtime.connect()
        # Transport preparation does not run doctor, open Xiaohongshu or create/resume a task.

    def _network_check(self):
        # Reading configuration can wait on the filesystem. Keep it off the
        # request thread and outside the controller lock so status/pause remain usable.
        self.runtime.ensure_not_cancelled()
        enabled = self.runtime.auto_connection_enabled()
        self.runtime.ensure_not_cancelled()
        if enabled:
            self.runtime.connect()

    def _database(self):
        self.runtime.database()
        self.store = DesktopStore(self.runtime.repo, self.runtime.settings.state_dir,
                                  self.runtime.device_id)
        with self.lock:
            self.state["initialized"] = True
        self.refresh()

    def _check(self):
        self._database()
        with self.lock:
            for check in self.state["device"]["checks"]:
                if check["id"] != "database":
                    check.update(status="unknown", message="等待本次检查")
            self.state["device"]["status"] = "checking"
            self.state["device"]["message"] = "正在检查手机与自动化服务"
        self.publish()
        self.runtime.device()
        with self.lock:
            self.state["device"]["status"] = "ready"
            self.state["device"]["message"] = "连接及适配检查通过；采集时仍会检查页面与会话"
            if self.state["issue"] and self.state["issue"]["code"] in CONNECTION_ISSUES | {
                "offline", "automation", "app_version", "busy", "database",
                "configuration", "timeout", "diagnostic",
            }:
                self.state["issue"] = None
        self.publish()

    def _collect(self, mode, keywords, limit):
        self._check()
        self.runtime.ensure_not_cancelled()
        if mode == "keywords":
            arguments = ["batch", "run", "--device", self.runtime.device_id, "--limit", str(limit),
                         *[f"--keyword={keyword}" for keyword in keywords]]
        else:
            arguments = ["collect-current", "--device", self.runtime.device_id]
        self._run(arguments)

    def _collect_resume(self, item, acknowledge):
        self._check()
        self.runtime.ensure_not_cancelled()
        arguments = (["batch", "resume", "--batch"] if item["kind"] == "batch"
                     else ["resume", "--task"]) + [item["id"]]
        if acknowledge:
            arguments.append("--acknowledge")
        self._run(arguments)

    def _run(self, arguments):
        self._activity(phase="running", message="正在采集，请保持 Mac 开机")
        code, values = self.runtime.cli(*arguments, collector=True)
        active = self.snapshot()["activity"]
        if not active or not active.get("id"):
            for value in values:
                self._progress(value)
            active = self.snapshot()["activity"]
        if active and active.get("id"):
            self.refresh()
            item = self._store().item(active["kind"], active["id"])
            self._result_issue(item, code, values)
            if not self.snapshot()["shutting_down"]:
                self._activity(phase="exporting", message="正在导出已保存结果")
                self._export(item)
        elif code:
            if self.runtime.cancelled.is_set() and any(
                value.get("error") == "DesktopParentGone" for value in values
            ):
                return
            if any(value.get("error") == "DeviceBusy" for value in values):
                raise DeviceBusy("Device already occupied")
            raise DesktopError(
                "execution", "采集未能启动，未确认任务编号，请先查看历史与诊断详情。",
            )

    def _result_issue(self, item, code, values):
        tasks = item["tasks"] or [item]
        reasons = " ".join(str(task.get("stop_reason") or "") for task in tasks)
        retry_error = next((value.get("message", "") for value in values
                            if str(value.get("message", "")).startswith("read_retry_check:")), "")
        if any(value.get("error") == "database_error" for value in values):
            self._issue("database", "数据保存发生错误，已停止采集，请检查本机数据库与诊断详情。")
        elif any(value.get("error") == "DeviceBusy" for value in values):
            self._issue("busy", "手机已被其他进程占用，本次任务保留，处理后可继续。")
        elif retry_error:
            detail = retry_error.removeprefix("read_retry_check:")
            if detail == "offline":
                self._issue("offline", "读取恢复检查确认手机已断开，请检查云手机后再继续。")
            elif detail == "cancelled":
                return
            elif detail in {"policy_blocked", "login_required", "captcha", "restricted",
                            "unknown", "rate_limited"}:
                self._issue("manual", "读取恢复检查发现页面或会话限制，请查看任务和手机。")
            else:
                self._issue("automation", "读取恢复检查未通过。"
                            "请查看手机页面及诊断详情，成果仍保留。")
        elif "evidence_save_failed" in reasons:
            self._issue("evidence", "证据未能可靠保存，已停止新增采集，请检查本机磁盘和诊断详情。")
        elif any(task["requires_ack"] for task in tasks):
            self._issue("manual", "会话或页面需要人工处理。请打开云手机，处理后检查并继续。")
        elif any(task.get("requires_read_check") for task in tasks):
            if "consecutive_no_progress" in reasons:
                message = "连续 10 次页面操作未取得进展，已暂停并保留成果。"
            elif "consecutive_read_failures" in reasons:
                message = "连续 10 次页面读取失败，已暂停并保留成果。"
            else:
                message = "此任务曾被旧版累计读取限制暂停；现已取消累计限制，成果保留。"
            self._issue("read_anomaly", message + "请核对手机页面，在任务中选择“检查并继续”。")
        elif "connection:" in reasons:
            reason = next((str(task.get("stop_reason", "")) for task in tasks
                           if str(task.get("stop_reason", "")).startswith("connection:")), "")
            failure = reason.removeprefix("connection:")
            if failure == "connection_recovery_exhausted":
                self._issue(failure, "本任务自动重连次数已用完。采集成果已保存，可查看和导出结果。")
            elif failure in CONNECTION_ISSUES:
                self._issue(failure, "手机自动重连未完成，成果已保存。请检查自动连接设置后继续。")
            else:
                self._issue("offline", "手机自动重连未完成，成果已保存。"
                            "请检查网络或打开云手机处理。")
        elif any(token in reasons for token in ("DeviceUnavailable", "DeviceError",
                                                "reopen_current_detail")):
            self._issue("automation", "页面读取或操作中断，成果已保存。"
                        "请检查手机页面及诊断详情后继续。")
        elif self._committed_operator_pause(item, values):
            with self.lock:
                self.state["issue"] = None
        elif code not in (0, 3):
            self._issue("execution", "采集未正常结束，已提交成果保留，请查看任务与诊断详情。")
        elif item["status"] == "collected_awaiting_review":
            with self.lock:
                self.state["issue"] = None

    def _committed_operator_pause(self, item, values):
        """A completed matching report and persisted task confirm a requested safe pause.

        A cancel request alone, or a stale report for another task, cannot disguise
        a process failure. Detailed failure/policy reasons take precedence above.
        """
        if (not self.runtime.cancelled.is_set() or item.get("status") != "paused"
                or item.get("stop_reason") != "operator_pause"):
            return False
        key = "batches" if item["kind"] == "batch" else "tasks"
        for value in reversed(values):
            for reported in value.get(key, []):
                if (reported.get("id") == item["id"] and reported.get("status") == "paused"
                        and reported.get("stop_reason") == "operator_pause"):
                    return True
        return False

    def _export(self, item):
        with self.lock:
            if self.state["shutting_down"]:
                return
            self.exporting = True
            self.export_activity = copy.deepcopy(self.state["activity"])
        try:
            records = self._store().records(item["kind"], item["id"])
            folder = self.runtime.settings.state_dir / "exports"
            if item["kind"] == "batch":
                folder /= "batches"
            folder = folder / item["id"] / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            if item["kind"] == "batch":
                batch = BatchRepository(self.runtime.repo).status(item["id"])["batches"][0]
            else:
                task = self.runtime.repo.status(item["id"])["tasks"][0]
                batch = {"id": item["id"], "status": task["status"], "tasks": [task]}
            # Both formats share exactly one frozen record list, even while an external
            # collector is committing. Completion manifest/INCOMPLETE is honored by the UI.
            export_bundle(records, batch, folder)
            with self.lock:
                if self.state["issue"] and self.state["issue"]["code"] == "export":
                    self.state["issue"] = None
            self.refresh()
        except Exception as exc:
            self.runtime.log_exception(exc)
            raise DesktopError(
                "export", "采集成果已保存，导出未完成。请重新导出，无需重新采集。",
            ) from exc
        finally:
            with self.lock:
                self.exporting = False
                self.export_activity = None

    def _reject_batch_child(self, identifier):
        for item in self._store().history():
            if any(child["id"] == identifier for child in item["tasks"]):
                raise ValueError("此任务属于批次，请从批次入口继续")

    def _pause_requested(self, params):
        active = self.snapshot()["activity"]
        kind, identifier = params.get("kind"), params.get("id")
        if active and (not identifier or (kind == active["kind"] and identifier == active["id"])):
            self._pause_current()
            self.publish()
            return
        item = self._store().item(kind, identifier)
        if kind == "batch":
            BatchRepository(self.runtime.repo).request_pause(identifier)
        else:
            self.runtime.repo.request_pause(identifier)
        if item["status"] not in {"running", "cooldown"}:
            return
        # This is a persisted request to an external runner, never an assertion it stopped.
        self.refresh()

    def _pause_current(self):
        with self.lock:
            active = copy.deepcopy(self.state["activity"])
            if active:
                self.state["activity"]["phase"] = "pausing"
                self.state["activity"]["message"] = "正在安全暂停，等待当前操作保存并结束"
            if self.state["shutting_down"] and self.exporting:
                # Export threads hold no phone and publish an INCOMPLETE marker before
                # writing. App exit may interrupt them; the database remains authoritative.
                self.state["activity"] = None
                self.state["busy"] = False
        self.runtime.pause()
        if active and active.get("id") and self.store is not None:
            try:
                if active["kind"] == "batch":
                    BatchRepository(self.runtime.repo).request_pause(active["id"])
                else:
                    self.runtime.repo.request_pause(active["id"])
            except Exception as exc:
                # The inherited control pipe remains a safe fallback if storage is failing.
                self.runtime.log_exception(exc)

    def _progress(self, value):
        event = value.get("event")
        if event == "connection_progress":
            with self.lock:
                self.state["device"]["connection_phase"] = value.get("phase")
                self.state["device"]["connection_deadline"] = (
                    None if value.get("phase") == "ready" else value.get("deadline_at")
                )
                if self.state["activity"]:
                    self.state["activity"]["message"] = value.get("message", "正在连接云手机")
            self.publish()
            return
        active = self.snapshot()["activity"]
        if not active:
            return
        changes = {}
        if value.get("batch_id") and active["kind"] == "batch":
            changes["id"] = value["batch_id"]
        if value.get("task_id"):
            changes["task_id"] = value["task_id"]
            if active["kind"] == "task":
                changes["id"] = value["task_id"]
        if event == "batch_task_started":
            changes["keyword"] = str(value.get("keyword", ""))
            changes["message"] = "正在采集：" + changes["keyword"]
        if changes:
            self._activity(**changes)
            # A pause before creation consumed no ID. Once committed, also persist it.
            if self.runtime.cancelled.is_set():
                self._pause_current()
            self.refresh()

    def _activity(self, **values):
        with self.lock:
            if self.state["activity"]:
                self.state["activity"].update(values)
        self.publish()

    def _check_update(self, key, status, message):
        with self.lock:
            for check in self.state["device"]["checks"]:
                if check["id"] == key:
                    check.update(status=status, message=message)
            if key == "connection":
                self.state["device"]["status"] = status
                self.state["device"]["message"] = message
                if status != "checking":
                    self.state["device"]["connection_deadline"] = None
            if key == "database" and status == "ready":
                self.state["initialized"] = True
            recovered = {
                "connection": CONNECTION_ISSUES | {"offline", "busy"},
                "automation": {"automation"},
                "app": {"app_version"}, "database": {"database"},
            }
            issue = self.state["issue"]
            if status == "ready" and issue and issue["code"] in recovered.get(key, set()):
                self.state["issue"] = None
        self.publish()

    def _issue(self, code, message):
        title, cloud = ISSUES.get(code, ISSUES["execution"])
        with self.lock:
            old = self.state["issue"]
            if not old or (old["code"], old["message"]) != (code, message):
                self.issue_sequence += 1
                token = f"{self.issue_sequence}:{code}:{message}".encode()
                digest = hashlib.sha256(token).hexdigest()[:20]
                self.state["issue"] = {
                    "id": digest, "code": code, "title": title, "message": message,
                    "cloud_action": cloud,
                    "can_retry": code not in {
                        "manual", "evidence", "execution", "read_anomaly",
                    },
                }
            # Deduplicate the popup ID, not the state transition. A retry may have
            # reset checks to checking even when its failure is the same incident.
            device = self.state["device"]
            if code in {"offline", "busy"}:
                device["status"] = code
                device["message"] = message
            elif cloud or code in CONNECTION_ISSUES:
                device["status"] = "attention"
                device["message"] = message
            target = {"offline": "connection", "busy": "connection", "automation": "automation",
                      "diagnostic": "automation", "app_version": "app",
                      "database": "database"}.get(code)
            if code in CONNECTION_ISSUES:
                target = "connection"
            for check in device["checks"]:
                if check["id"] == target:
                    check.update(status="attention" if code != "offline" else "offline",
                                 message=message)
        self.publish()

    def refresh(self):
        if self.store is None:
            return
        try:
            items = self.store.history(self.snapshot()["activity"])
            with self.lock:
                self.state["tasks"] = items
        except Exception as exc:
            self.runtime.log_exception(exc)
            self._issue("database", "本机数据读取失败。已显示内容可能不是最新状态，请重新检查。")

    def _monitor(self):
        while not self.closed.wait(3):
            self.refresh()
            self.publish()

    def close(self):
        self.closed.set()
        with self.lock:
            self.state["shutting_down"] = True
        self._pause_current()
        if self.worker and self.worker.is_alive() and not self.exporting:
            self.worker.join()  # Collector must really exit; never declare an early safe shutdown.
        if self.runtime.repo:
            self.runtime.repo.engine.dispose()
