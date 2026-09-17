"""Local, bounded cloud-phone connection and recoverable /32 authorization.

The native helper owns Keychain access and the deliberately narrow cloud API.
This module never receives an AccessKey, secret, signed request, or cloud token.
All transport changes share the collector's physical-device lock.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import plistlib
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .locking import DeviceLock


class ConnectionFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def public_cidr(value: str) -> str:
    """A candidate is exactly one public IPv4; never a wider network."""
    try:
        if not isinstance(value, str):
            raise ValueError("an address string is required")
        network = ipaddress.IPv4Network(value, strict=True)
    except (ValueError, TypeError) as exc:
        raise ConnectionFailure("configuration", "连接地址必须是单个公网 IPv4。") from exc
    address = network.network_address
    if (network.prefixlen != 32 or not address.is_global or address.is_multicast
            or address in ipaddress.IPv4Network("192.0.0.0/24")
            or address in ipaddress.IPv4Network("192.88.99.0/24")):
        raise ConnectionFailure("configuration", "连接地址必须是单个公网 IPv4 /32。")
    return str(network)


def atomic_private_json(path: Path, value: dict) -> None:
    """Journal changes reach disk before the corresponding external action."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ConnectionFailure("configuration", "连接状态目录不能使用符号链接。")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class ConnectionManager:
    def __init__(self, settings, device_id, *, progress=None, stop_requested=None, project=None):
        self.settings = settings
        self.device_id = device_id
        self.project = Path(project or settings.state_dir.parent).resolve()
        self.progress = progress or (lambda value: None)
        self.stop_requested = stop_requested or (lambda: False)
        if (not isinstance(device_id, str) or not device_id or device_id in {".", ".."}
                or any(char in "/\\" or ord(char) < 32 for char in device_id)):
            raise ConnectionFailure("configuration", "设备标识无效。")
        self.record_path = settings.state_dir / "connection" / f"mobile-{device_id}.json"
        self.route_path = self.record_path.with_name(f"mobile-{device_id}-route.json")
        self.journal_path = self.record_path.with_name(f"mobile-{device_id}-journal.json")
        self.open_route_journal_path = self.record_path.with_name(
            f"mobile-{device_id}-open-route-journal.json",
        )
        self.record = {}
        self._configuration_error = False
        try:
            self.record = self._read_document(self.record_path)
        except ConnectionFailure:
            self._configuration_error = True
        self.adb = self.record.get("adb", {})
        self.auto = self.record.get("auto_connection", {})
        if not isinstance(self.adb, dict) or not isinstance(self.auto, dict):
            self._configuration_error = True
            self.adb, self.auto = {}, {}
        self.auto_enabled = self.auto.get("enabled") is True
        self.deadline = None

    @property
    def authorization_mode(self):
        mode = self.auto.get("authorization_mode", "prefix_list")
        if mode not in ("open", "prefix_list"):
            raise ConnectionFailure("configuration", "连接授权模式无效，请检查连接设置。")
        return mode

    @staticmethod
    def _read_document(path: Path) -> dict:
        try:
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError("invalid document")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid document")
            return value
        except (OSError, ValueError) as exc:
            raise ConnectionFailure("configuration", "连接设置无法读取，请打开连接设置。") from exc

    @property
    def serial(self):
        value = self.adb.get("local_serial", "")
        if self._configuration_error or not isinstance(value, str) or not re.fullmatch(
            r"127\.0\.0\.1:[0-9]{1,5}", value,
        ) or not 1 <= int(value.rsplit(":", 1)[1]) <= 65535:
            raise ConnectionFailure("configuration", "请先配置本机回环 ADB 连接。")
        return value

    def _check(self):
        if self.stop_requested():
            raise ConnectionFailure("cancelled", "连接操作已取消，已有成果仍保留。")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ConnectionFailure("connection_timeout", "本次连接已超时，请处理后重试。")

    def _remaining(self, maximum):
        self._check()
        return min(maximum, self.deadline - time.monotonic()) if self.deadline else maximum

    def _sleep(self, seconds):
        until = time.monotonic() + self._remaining(seconds)
        while time.monotonic() < until:
            self._check()
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    def _emit(self, phase, message, **extra):
        self._check()
        deadline = None
        if self.deadline is not None:
            deadline = (datetime.now(UTC) + timedelta(
                seconds=max(0, self.deadline - time.monotonic()),
            )).isoformat()
        self.progress({"phase": phase, "message": message, "deadline_at": deadline, **extra})

    def _command(self, argv, timeout, *, input_bytes=None):
        """Drain both pipes while polling cancellation; never expose raw stderr."""
        duration = self._remaining(timeout)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ConnectionFailure("configuration", "本机连接组件无法启动，请检查安装。") from exc
        try:
            pending = input_bytes
            while True:
                self._check()
                left = duration - (time.monotonic() - started)
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv[0], duration)
                try:
                    stdout, stderr = process.communicate(input=pending, timeout=min(0.15, left))
                    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pending = None
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def transport_healthy(self, *, lock_held=False):
        """Read-only ADB state plus shell roundtrip, with no reconnect or cloud calls."""
        lock = contextlib.nullcontext() if lock_held else DeviceLock(
            self.serial, self.settings.state_dir,
        )
        with lock:
            try:
                state = self._command([self.settings.adb_path, "-s", self.serial, "get-state"], 4)
                self._check_adb_authorization(state)
                if state.returncode or state.stdout.strip() != b"device":
                    return False
                probe = self._command([
                    self.settings.adb_path, "-s", self.serial, "shell", "echo", "xhs-link-ok",
                ], 4)
                return probe.returncode == 0 and probe.stdout.strip() == b"xhs-link-ok"
            except subprocess.TimeoutExpired:
                return False

    @staticmethod
    def _check_adb_authorization(result):
        message = (result.stdout + result.stderr).lower()
        if b"unauthorized" in message or b"failed to authenticate" in message:
            raise ConnectionFailure(
                "authorization", "手机尚未授权本机 ADB，请打开云手机检查绑定的连接密钥。",
            )

    def _helper(self, operation, cidr=None):
        path = self.auto.get("helper_path") or str(
            Path.home() / "Applications/小红书采集助手.app/Contents/MacOS/XHSMobileDesktop",
        )
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ConnectionFailure("configuration", "原生连接助手路径无效。")
        request = {"operation": operation, "timeout_seconds": self._remaining(12)}
        if cidr is not None:
            request["cidr"] = public_cidr(cidr)
        try:
            result = self._command(
                [path, "--connection-helper", "--record", str(self.record_path)], 13,
                input_bytes=json.dumps(request).encode(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ConnectionFailure("cloud_timeout", "云端授权检查超时，更新记录已保留。") from exc
        try:
            if len(result.stdout) > 65536:
                raise ValueError("oversize response")
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("invalid helper response")
        except (ValueError, TypeError) as exc:
            raise ConnectionFailure("cloud_error", "连接授权助手未返回有效结果。") from exc
        if value.get("ok") is not True or result.returncode:
            code = value.get("code")
            messages = {
                "credentials": "请在连接设置中完成钥匙串授权或更新凭据。",
                "authorization": "连接授权权限不足，请检查专用云权限设置。",
                "configuration": "自动连接名单或关联关系不匹配，请检查连接设置。",
                "cloud_timeout": "云端授权检查超时，更新记录已保留。",
            }
            if code not in messages:
                code = "cloud_error"
            raise ConnectionFailure(code, messages.get(code, "云端连接授权未完成，请重试检查。"))
        return value

    def _inspect(self):
        value = self._helper("inspect")
        entries = value.get("entries")
        if (
            not isinstance(entries, list) or len(entries) > 2
            or value.get("max_entries") != 2 or value.get("associated") is not True
        ):
            raise ConnectionFailure("configuration", "自动连接名单容量或关联关系不符合要求。")
        checked = [public_cidr(entry) for entry in entries]
        if len(set(checked)) != len(checked):
            raise ConnectionFailure("configuration", "自动连接名单包含重复地址。")
        return checked

    def _write_journal(self, journal):
        atomic_private_json(self.journal_path, journal)

    def _clear_journal(self):
        # An explicit durable empty journal avoids deletion/write ordering gaps.
        self._write_journal({"version": 1, "phase": "idle"})

    def _load_journal(self):
        if not self.journal_path.exists():
            return None
        journal = self._read_document(self.journal_path)
        if journal.get("version") != 1:
            raise ConnectionFailure("journal_pending", "连接更新记录版本不匹配，请检查设置。")
        if journal.get("phase") == "idle":
            return None
        if (
            journal.get("phase") not in {"adding", "verified", "rolling_back"}
            or journal.get("prefix_list_id") != self.auto.get("prefix_list_id")
            or not isinstance(journal.get("old_entries"), list)
            or not self._valid_route(journal.get("previous_route"))
        ):
            raise ConnectionFailure("journal_pending", "连接更新记录与当前名单不匹配。")
        public_cidr(journal.get("candidate"))
        old = journal["old_entries"]
        if (len(old) > 2 or any(not isinstance(value, str) for value in old)
                or len(set(old)) != len(old)):
            raise ConnectionFailure("journal_pending", "连接更新记录中的原地址无效。")
        for value in old:
            public_cidr(value)
        return journal

    @staticmethod
    def _valid_route(value):
        return isinstance(value, str) and (
            value == "system" or re.fullmatch(r"en[0-9]{1,3}", value) is not None
        )

    def _previous_route(self):
        if self.route_path.exists():
            value = self._read_document(self.route_path).get("interface")
        else:
            value = self.adb.get("outbound_interface", "system")
        if not self._valid_route(value):
            raise ConnectionFailure("configuration", "已保存的网络路线无效。")
        return value

    def _wait_entries(self, predicate):
        until = time.monotonic() + self._remaining(8)
        while True:
            entries = self._inspect()
            if predicate(entries):
                return True
            if time.monotonic() >= until:
                return False
            self._sleep(0.4)

    def _remove_and_confirm(self, cidr):
        """A timeout is ambiguous: inspect before deciding whether removal failed."""
        try:
            self._helper("remove_candidate", cidr)
        except ConnectionFailure as exc:
            if exc.code not in {"cloud_error", "cloud_timeout"}:
                raise
        if not self._wait_entries(lambda entries: cidr not in entries):
            raise ConnectionFailure("journal_pending", "旧连接地址清理尚未完成，请重新检查连接。")

    def _reconcile(self, journal=None):
        journal = journal or self._load_journal()
        if journal is None:
            return
        self._emit("authorization", "正在核对上次连接授权")
        current = self._inspect()
        candidate, old = journal["candidate"], journal["old_entries"]
        if set(current) - (set(old) | {candidate}):
            raise ConnectionFailure("journal_pending", "名单在连接更新期间发生变化，请检查设置。")
        if journal["phase"] == "verified":
            if candidate not in current:
                raise ConnectionFailure("journal_pending", "已验证的连接地址已改变，请检查设置。")
            for value in old:
                if value != candidate and value in current:
                    self._remove_and_confirm(value)
        elif candidate not in old and candidate in current:
            self._write_journal({**journal, "phase": "rolling_back"})
            self._remove_and_confirm(candidate)
        if journal["phase"] != "verified":
            atomic_private_json(self.route_path, {"interface": journal["previous_route"]})
        self._clear_journal()

    def _restore_pending_route(self):
        """Local cancellation rollback is safe even when no further API is allowed."""
        journal = self._load_journal()
        if journal is not None and journal["phase"] != "verified":
            atomic_private_json(self.route_path, {"interface": journal["previous_route"]})

    def _open_route_journal(self):
        """Local route recovery is independent of former prefix-list transactions."""
        if not self.open_route_journal_path.exists():
            return None
        journal = self._read_document(self.open_route_journal_path)
        if journal.get("version") == 1 and journal.get("phase") == "idle":
            return None
        if (
            journal.get("version") != 1
            or journal.get("phase") not in {"switching", "verified"}
            or not self._valid_route(journal.get("previous_route"))
            or not self._valid_route(journal.get("interface"))
        ):
            raise ConnectionFailure("configuration", "本机网络路线恢复记录无效，请检查连接设置。")
        return journal

    def _restore_open_route(self):
        journal = self._open_route_journal()
        if journal is None:
            return
        if journal["phase"] == "switching":
            atomic_private_json(self.route_path, {"interface": journal["previous_route"]})
        atomic_private_json(self.open_route_journal_path, {"version": 1, "phase": "idle"})

    def _try_open_route(self, interface):
        journal = {
            "version": 1, "phase": "switching", "interface": interface,
            "previous_route": self._previous_route(),
        }
        atomic_private_json(self.open_route_journal_path, journal)
        try:
            atomic_private_json(self.route_path, {"interface": interface})
            self._emit(
                "connecting", "正在连接云手机 · 开放连接，无需地址授权", interface=interface,
            )
            if not self._wait_transport():
                raise ConnectionFailure("offline", "云手机采集连接尚未建立。")
            self._check()
            atomic_private_json(self.open_route_journal_path, {**journal, "phase": "verified"})
        except BaseException:
            # No cancellable subprocess or API is needed to restore the route.
            # If this write fails, the durable switching journal remains for restart.
            self._restore_open_route()
            raise
        self._restore_open_route()

    def _ensure_relay(self):
        service = self.adb.get("launchd_service", "")
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service):
            raise ConnectionFailure("configuration", "本机云手机连接服务尚未配置。")
        plist = Path.home() / "Library/LaunchAgents" / f"{service}.plist"
        if self.auto_enabled:
            try:
                value = plistlib.loads(plist.read_bytes())
                arguments = value["ProgramArguments"]
                index = arguments.index("--route-config")
                if Path(arguments[index + 1]).resolve() != self.route_path.resolve():
                    raise ValueError("wrong route configuration")
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                raise ConnectionFailure(
                    "configuration", "本机连接服务需要升级，请重新运行安装程序。",
                ) from exc
        domain = f"gui/{os.getuid()}"
        state = self._command(["/bin/launchctl", "print", f"{domain}/{service}"], 5)
        if state.returncode:
            if not plist.is_file() or self._command(
                ["/bin/launchctl", "bootstrap", domain, str(plist)], 8,
            ).returncode:
                raise ConnectionFailure("offline", "本机连接服务无法启动，请查看诊断详情。")

    def candidate_interfaces(self):
        """System first, then active physical interfaces in macOS service order."""
        result = ["system"]
        try:
            order = self._command(["/usr/sbin/networksetup", "-listnetworkserviceorder"], 4)
            for name in re.findall(rb"Device: (en[0-9]{1,3})\)", order.stdout):
                name = name.decode("ascii")
                if name in result:
                    continue
                state = self._command(["/sbin/ifconfig", name], 3)
                if not state.returncode and re.search(rb"\binet \d", state.stdout) and (
                    b"status: active" in state.stdout
                ):
                    result.append(name)
                if len(result) == 3:
                    break
        except subprocess.TimeoutExpired:
            pass
        return result

    def _public_address(self, interface):
        arguments = ["/usr/bin/curl", "-q", "-4", "--noproxy", "*", "--fail", "--silent",
                     "--show-error", "--max-time", "7", "--connect-timeout", "4"]
        if interface != "system":
            arguments.extend(["--interface", interface])
        result = self._command([*arguments, "https://checkip.amazonaws.com/"], 8)
        if result.returncode:
            raise ConnectionFailure("offline", "当前网络出口无法识别，正在尝试其他连接路线。")
        try:
            value = result.stdout.decode("ascii").strip()
            address = ipaddress.IPv4Address(value)
        except (ValueError, UnicodeError) as exc:
            raise ConnectionFailure("offline", "当前网络未返回有效公网地址。") from exc
        return public_cidr(f"{address}/32")

    def _connect_transport(self):
        # Replacing only this serial cannot affect other ADB devices.
        self._command([self.settings.adb_path, "disconnect", self.serial], 4)
        result = self._command([self.settings.adb_path, "connect", self.serial], 9)
        self._check_adb_authorization(result)
        return self.transport_healthy(lock_held=True)

    def _wait_transport(self):
        # Security-group propagation can lag list readback. Poll within this one
        # route attempt; task recovery budgets are charged for route candidates.
        until = time.monotonic() + self._remaining(12)
        while True:
            if self._connect_transport():
                return True
            if time.monotonic() >= until:
                return False
            self._sleep(0.6)

    def _try_route(self, interface):
        self._emit("network", "正在识别当前网络出口", interface=interface)
        cidr = self._public_address(interface)
        self._emit("authorization", "正在核对连接授权", interface=interface)
        old = self._inspect()
        if len(old) == 2 and cidr not in old:
            raise ConnectionFailure("configuration", "连接名单已满，请在设置中核对已有地址。")
        journal = {
            "version": 1, "phase": "adding", "prefix_list_id": self.auto.get("prefix_list_id"),
            "candidate": cidr, "old_entries": old, "interface": interface,
            "previous_route": self._previous_route(),
        }
        self._write_journal(journal)
        if cidr not in old:
            self._emit("authorization", "正在更新当前网络的连接授权", interface=interface)
            try:
                self._helper("add_candidate", cidr)
            except ConnectionFailure as exc:
                if exc.code not in {"cloud_timeout", "cloud_error"}:
                    raise
            if not self._wait_entries(lambda entries: cidr in entries):
                raise ConnectionFailure("cloud_timeout", "当前网络授权尚未生效，请重试连接。")
        atomic_private_json(self.route_path, {"interface": interface})
        self._emit("connecting", "正在建立手机采集连接", interface=interface)
        if not self._wait_transport():
            raise ConnectionFailure("offline", "网络可达但手机采集连接未建立。")
        # Persist verified identity before cleaning old authorization. An exit at
        # this point preserves both addresses until the next startup reconciles.
        journal["phase"] = "verified"
        self._write_journal(journal)
        self._reconcile(journal)

    def ensure_connected(self, *, lock_held=False, consume_attempt=None):
        lock = contextlib.nullcontext() if lock_held else DeviceLock(
            self.serial, self.settings.state_dir,
        )
        self.deadline = time.monotonic() + 120
        try:
            with lock:
                self._check()
                open_mode = self.authorization_mode == "open"
                if self.auto_enabled and open_mode:
                    self._restore_open_route()
                elif self.auto_enabled:
                    self._reconcile()
                if self.transport_healthy(lock_held=True):
                    self._emit("connected", "云手机已连接")
                    return self.serial
                self._emit("preparing", "正在准备手机连接")
                self._ensure_relay()
                candidates = self.candidate_interfaces() if self.auto_enabled else [None] * 3
                last = ConnectionFailure("offline", "本次连接未建立，请处理后重试。")
                for index, interface in enumerate(candidates[:3]):
                    self._check()
                    if consume_attempt is not None and not consume_attempt():
                        raise ConnectionFailure(
                            "connection_recovery_exhausted",
                            "本任务连接恢复次数已用完，成果已保留。",
                        )
                    self._emit("connecting", f"正在连接手机（第 {index + 1} / 3 次）",
                               attempt=index + 1)
                    try:
                        if self.auto_enabled and open_mode:
                            self._try_open_route(interface)
                        elif self.auto_enabled:
                            self._try_route(interface)
                        elif not self._connect_transport():
                            raise ConnectionFailure("offline", "云手机采集连接尚未建立。")
                        self._check()
                        self._emit("connected", "云手机已连接", interface=interface)
                        return self.serial
                    except subprocess.TimeoutExpired:
                        if self.auto_enabled and not open_mode:
                            self._restore_pending_route()
                        last = ConnectionFailure("offline", "手机连接等待超时，请处理后重试。")
                    except ConnectionFailure as exc:
                        if self.auto_enabled and not open_mode:
                            self._restore_pending_route()
                        if exc.code not in {"offline", "cloud_timeout", "cloud_error"}:
                            raise
                        last = exc
                    if self.auto_enabled and not open_mode:
                        self._reconcile()
                raise last
        except subprocess.TimeoutExpired as exc:
            raise ConnectionFailure("connection_timeout", "本次连接检查已超时，请重试。") from exc
        except OSError as exc:
            raise ConnectionFailure(
                "configuration", "本机连接状态无法可靠保存，操作已停止。",
            ) from exc
        finally:
            self.deadline = None


def network_fingerprint(command=None):
    """Cheap local observation only; no device, reconnect, or external requests."""
    command = command or subprocess.run
    try:
        result = command(
            ["/usr/sbin/scutil", "--nwi"], capture_output=True, timeout=3, check=False,
        )
        return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
