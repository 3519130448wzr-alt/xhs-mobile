"""Desktop process adapter. No signal handlers are installed in worker threads."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from xhs_mobile.connection import ConnectionFailure, ConnectionManager
from xhs_mobile.domain import DeviceBusy
from xhs_mobile.locking import DeviceLock
from xhs_mobile.profile import load_profile
from xhs_mobile.repository import Repository


class DesktopError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class DesktopRuntime:
    """Reuse the terminal launcher's configured private local database and relay.

    The desktop bridge puts the project's scripts folder on its import path. This
    adapter uses the launcher's configuration and connection parsing only; its
    blocking terminal menu and signal-installing execute method are never used.
    """

    def __init__(self, project: Path, device_id="lab01"):
        from launcher import Launcher, documents

        self.launcher = Launcher(project, device_id=device_id)
        self.parse_documents = documents
        self.settings = self.launcher.settings
        self.device_config = self.launcher.device_config
        self.device_id = device_id
        self.project = project.resolve()
        self.log_dir = self.launcher.log_dir
        self.env = None
        self.repo = None
        self.process = None
        self.control_write = None
        self.process_is_collector = False
        self._pause_process = None
        self._lock = threading.RLock()
        self._counter = 0
        self.cancelled = threading.Event()
        self.progress = lambda value: None
        self.check_update = lambda key, status, message: None

    def log_exception(self, error):
        import traceback

        with (self.log_dir / "desktop-errors.log").open("a", encoding="utf-8") as stream:
            traceback.print_exception(error, file=stream)
        (self.log_dir / "desktop-errors.log").chmod(0o600)

    def ensure_not_cancelled(self):
        if self.cancelled.is_set():
            raise DesktopError("cancelled", "已取消本次启动，已有记录仍保留。")

    def database(self):
        from local_postgres import LocalDatabaseError

        self.check_update("database", "checking", "正在准备本机数据库")
        try:
            with self.launcher.database.lock():
                self.launcher.database.start()
                if not self.launcher.database.status().get("running"):
                    raise DesktopError("database", "本机数据库未能启动，请查看诊断详情。")
                self.env = self.launcher.database.app_environment()
            self.ensure_not_cancelled()
            code, _ = self.cli("db", "upgrade", timeout=90)
            if code:
                raise DesktopError("database", "数据库准备未完成，请查看诊断详情。")
            if self.repo:
                self.repo.engine.dispose()
            self.repo = Repository.connect(self.env[self.settings.database_url_env])
            self.check_update("database", "ready", "本机数据已就绪")
        except (LocalDatabaseError, OSError, subprocess.SubprocessError) as exc:
            self.log_exception(exc)
            raise DesktopError("database", "数据库准备失败，请检查本机存储与诊断日志。") from exc

    def observe_connection(self):
        """One startup observation; never reconnect, install helpers or inspect App pages.

        This must not be called by periodic history refreshes. Acquiring the same
        physical-device lock also prevents even this passive check from competing
        with a collector already using the phone.
        """
        from launcher import LauncherError

        self.ensure_not_cancelled()
        self.check_update("connection", "checking", "正在查看云手机连接状态")
        try:
            _, serial = self.launcher.connection()
            with DeviceLock(serial, self.settings.state_dir):
                state = self._command([self.settings.adb_path, "-s", serial, "get-state"], 15)
            self.ensure_not_cancelled()
            if state.returncode or state.stdout.strip() != b"device":
                message = "云手机尚未连接，可打开云手机处理后重新检查。"
                self.check_update("connection", "offline", message)
                raise DesktopError("offline", message)
            self.check_update("connection", "ready", "ADB 已连接；自动化服务与登录尚未检查")
        except DeviceBusy:
            self.check_update("connection", "busy", "设备正在由其他程序使用，请查看任务记录")
            raise
        except (LauncherError, ValueError) as exc:
            self.log_exception(exc)
            message = "云手机接入配置未能读取，请查看诊断详情。"
            self.check_update("connection", "offline", message)
            raise DesktopError("configuration", message) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self.log_exception(exc)
            message = "云手机连接检查未完成，可打开云手机处理后重新检查。"
            self.check_update("connection", "offline", message)
            raise DesktopError("offline", message) from exc

    def connection_manager(self):
        return ConnectionManager(
            self.settings, self.device_id, project=self.project,
            progress=self._connection_progress, stop_requested=self.cancelled.is_set,
        )

    def auto_connection_enabled(self):
        try:
            return self.connection_manager().auto_enabled
        except ConnectionFailure:
            return False

    def _connection_progress(self, value):
        self.check_update("connection", "checking", value.get("message", "正在连接云手机"))
        self.progress({"event": "connection_progress", **value})

    def connect(self):
        """Prepare transport only; never open the App or start/resume a task."""
        self.ensure_not_cancelled()
        try:
            manager = self.connection_manager()
            serial = manager.ensure_connected()
            self.ensure_not_cancelled()
            message = ("开放连接，无需地址授权；手机已连接" if manager.authorization_mode == "open"
                       else "手机已连接；自动化服务与登录尚未检查")
            self.check_update("connection", "ready", message)
            return serial
        except ConnectionFailure as exc:
            self.check_update("connection", "offline" if exc.code == "offline" else "attention",
                              str(exc))
            raise DesktopError(exc.code, str(exc)) from exc

    def device(self):
        from launcher import LauncherError

        self.ensure_not_cancelled()
        self.check_update("connection", "checking", "正在检查云手机连接")
        try:
            profile = load_profile(self.settings.profile_path)
            _, serial = self.launcher.connection()
        except (LauncherError, ValueError, OSError) as exc:
            self.log_exception(exc)
            raise DesktopError(
                "configuration", "设备接入或页面配置不完整，请查看诊断详情。"
            ) from exc
        if profile.app_package != self.device_config.app_package:
            raise DesktopError("app_version", "页面规则与 App 配置不匹配，需要完成适配。")
        self.env[self.device_config.serial_env] = serial
        try:
            self.connect()
            self.check_update("automation", "checking", "正在检查自动化服务")
            code, values = self.cli("doctor", "--device", self.device_id, timeout=180)
            if any(value.get("error") == "DeviceBusy" for value in values):
                raise DeviceBusy("Another controller owns this device")
            health = next((value["health"] for value in values if "health" in value), {})
            checks = health.get("checks", {}) if isinstance(health, dict) else {}
            checks = checks if isinstance(checks, dict) else {}
            if checks.get("adb_state") != "device":
                self._verify_diagnostic_connection()
                raise DesktopError("diagnostic", "手机已连接，但诊断未返回完整结果。"
                                   "请查看诊断详情并重新检查。")
            if not checks.get("package_installed"):
                raise DesktopError("app_version", "云手机已连接，但未找到配置的小红书 App。")
            if checks.get("app_version") != profile.app_version:
                raise DesktopError("app_version", "云手机已连接，小红书版本需要重新适配。")
            self.check_update("app", "ready", f"已适配小红书 {profile.app_version}")
            if code or not health.get("ok"):
                raise DesktopError("automation", "云手机已连接，自动化服务检查未通过。")
            self.check_update("automation", "ready", "自动化服务正常；登录状态在采集时确认")
            self.ensure_not_cancelled()
        except DeviceBusy:
            raise
        except DesktopError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            self.log_exception(exc)
            raise DesktopError("diagnostic", "手机诊断未完成，请查看诊断详情并重新检查。") from exc

    def _verify_diagnostic_connection(self):
        """Missing doctor JSON is not offline evidence; probe only explicit transport.

        This finite read-only check neither reconnects nor updates authorization.
        It is never used by the periodic database-only status refresh.
        """
        self.ensure_not_cancelled()
        try:
            healthy = self.connection_manager().transport_healthy()
        except ConnectionFailure as exc:
            raise DesktopError(exc.code, str(exc)) from exc
        if not healthy:
            raise DesktopError("offline", "手机连接检查失败，请打开云手机处理后重新检查。")
        self.check_update("connection", "ready", "手机连接正常；诊断结果需要重新检查")

    def _command(self, arguments, timeout):
        self.ensure_not_cancelled()
        return subprocess.run(
            arguments, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        )

    def cli(self, *arguments, timeout=None, collector=False):
        if self.env is None:
            raise DesktopError("database", "本机数据库尚未就绪。")
        return self.execute([
            sys.executable, "-m", "xhs_mobile.cli", "--config",
            str(self.launcher.config_path), *arguments,
        ], timeout=timeout, collector=collector)

    def execute(self, arguments, *, timeout=None, collector=False):
        self.ensure_not_cancelled()
        if collector and timeout is not None:
            raise ValueError("A collector must use safe pause, not a forced process timeout")
        with self._lock:
            self._counter += 1
            log = self.log_dir / f"desktop-{self._counter:04d}.log"
        read_fd = write_fd = None
        process = None
        env = dict(self.env or os.environ)
        env.pop("XHS_DESKTOP_CONTROL_FD", None)
        if collector:
            read_fd, write_fd = os.pipe()
            env["XHS_DESKTOP_CONTROL_FD"] = str(read_fd)
        try:
            with log.open("x", encoding="utf-8") as output:
                log.chmod(0o600)
                with self._lock:
                    self.process = subprocess.Popen(
                        arguments, cwd=self.project, env=env, stdout=output,
                        # Only the bridge reads App JSONL requests. Children use
                        # the dedicated control FD for safe pause, never stdin.
                        stdin=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT, start_new_session=True,
                        pass_fds=(read_fd,) if read_fd is not None else (),
                    )
                    self.process_is_collector = collector
                    self.control_write = write_fd
                    write_fd = None  # ownership transfers only after Popen succeeds
                    process = self.process
                if read_fd is not None:
                    os.close(read_fd)
                    read_fd = None
                if collector and self.cancelled.is_set():
                    self.pause()
                seen = set()
                started = time.monotonic()
                while process.poll() is None:
                    self._progress(log, seen)
                    if timeout is not None and time.monotonic() - started > timeout:
                        # Only finite setup commands have a timeout. Never force-kill a
                        # collector: its pipe requests an ordinary checkpointed pause.
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise DesktopError("timeout", "准备步骤超时，请查看诊断详情并重新检查。")
                    try:
                        process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                self._progress(log, seen)
            return process.returncode, self.parse_documents(log.read_text(
                encoding="utf-8", errors="replace"
            ))
        finally:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                # Popen/log setup can fail before the write end becomes active.
                os.close(write_fd)
            with self._lock:
                if self.process is process and self.control_write is not None:
                    os.close(self.control_write)
                    self.control_write = None
            if process is not None and process.poll() is None:
                if collector:
                    # A failed progress callback/log read must not report idle
                    # while a phone operation is still running. Closing the pipe
                    # requests a pause even before the child installs handlers.
                    process.wait()
                else:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            with self._lock:
                if self.process is process:
                    self.process = None
                    self.process_is_collector = False
                if getattr(self, "_pause_process", None) is process:
                    self._pause_process = None

    def _progress(self, path, seen):
        for index, value in enumerate(self.parse_documents(path.read_text(
            encoding="utf-8", errors="replace"
        ))):
            event = value.get("event")
            if event not in {"task_created", "run_started", "batch_created", "batch_started",
                             "batch_task_started", "batch_task_finished", "connection_progress"}:
                continue
            key = (event, value.get("batch_id"), value.get("task_id"), value.get("run_id"))
            if event == "connection_progress":
                # The append-only log position preserves every phase/attempt,
                # including a repeated phase, without replaying it on each poll.
                key = (event, index)
            if key not in seen:
                seen.add(key)
                self.progress(value)

    def pause(self):
        self.cancelled.set()
        with self._lock:
            if self.process is not None and getattr(self, "_pause_process", None) is self.process:
                return
            if self.control_write is not None:
                # Closing the pipe works even before the child's signal handler exists.
                os.close(self.control_write)
                self.control_write = None
                self._pause_process = self.process
            elif self.process is not None and self.process_is_collector:
                with contextlib.suppress(ProcessLookupError):
                    self.process.send_signal(signal.SIGINT)
                self._pause_process = self.process
