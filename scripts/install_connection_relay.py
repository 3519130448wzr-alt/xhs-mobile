"""Migrate only this project's existing user relay; never connect ADB or the cloud."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xhs_mobile.config import load_settings
from xhs_mobile.connection import ConnectionManager, atomic_private_json
from xhs_mobile.locking import DeviceLock


class RelayInstallError(RuntimeError):
    pass


def _command(arguments):
    return subprocess.run(arguments, capture_output=True, timeout=12, check=False)


def loaded_arguments(result):
    """Read the structured arguments block shown by launchctl print."""
    if result.returncode:
        return None
    lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    reading, arguments = False, []
    for line in lines:
        value = line.strip()
        if not reading:
            if value == "arguments = {":
                reading = True
            continue
        if value == "}":
            return arguments
        if value:
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            arguments.append(value)
    raise RelayInstallError("无法核对已加载的连接服务参数，未继续安装。")


def _write_bytes(path, content, mode=0o600):
    if path.is_symlink() or path.parent.is_symlink():
        raise RelayInstallError("连接服务文件不能使用符号链接。")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _validate_owned(project, manager, document):
    service = manager.adb.get("launchd_service")
    arguments = document.get("ProgramArguments")
    if (
        not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service)
        or document.get("Label") != service or "Program" in document
        or not isinstance(arguments, list) or len(arguments) < 2
        or not all(isinstance(value, str) for value in arguments)
    ):
        raise RelayInstallError("现有连接服务不属于已配置设备，未修改。")
    script = project / "scripts/adb_interface_relay.py"
    interpreters = {(project / ".venv/bin/python").resolve(), Path(sys.executable).resolve()}
    if (not Path(arguments[0]).is_absolute() or Path(arguments[0]).resolve() not in interpreters
            or not Path(arguments[1]).is_absolute() or Path(arguments[1]).resolve() != script):
        raise RelayInstallError("现有服务没有运行本项目的连接程序，未修改。")
    known = {"--interface", "--remote-host", "--remote-port", "--listen-host", "--listen-port",
             "--connect-timeout", "--buffer-size", "--route-config"}
    if (len(arguments) - 2) % 2:
        raise RelayInstallError("现有连接服务参数无法安全迁移。")
    options = {}
    for name, value in zip(arguments[2::2], arguments[3::2], strict=True):
        if name not in known or name in options:
            raise RelayInstallError("现有连接服务包含未知或重复参数，未修改。")
        options[name] = value
    interface = options.get("--interface")
    if not ConnectionManager._valid_route(interface):
        raise RelayInstallError("现有服务的网络接口无效，未修改。")
    try:
        actual_port = int(options.get("--remote-port", "0"))
        expected_port = int(manager.adb.get("remote_port", 0))
        listen_port = int(options.get("--listen-port", "6100"))
        serial_port = int(manager.serial.rsplit(":", 1)[1])
    except (ValueError, TypeError) as exc:
        raise RelayInstallError("现有连接服务端口无效，未修改。") from exc
    if (
        options.get("--remote-host") != manager.adb.get("remote_host")
        or not options.get("--remote-host") or not 1 <= actual_port == expected_port <= 65535
        or options.get("--listen-host", "127.0.0.1") != "127.0.0.1"
        or not 1 <= listen_port == serial_port <= 65535
    ):
        raise RelayInstallError("现有服务的目标或监听地址与设备记录不同，未修改。")
    if "--route-config" in options:
        if Path(options["--route-config"]).resolve() != manager.route_path.resolve():
            raise RelayInstallError("现有服务使用另一份路线配置，未修改。")
    return list(arguments), interface


def _restore(plist, original, mode, route, old_route, domain, target, was_loaded, old_arguments):
    """Restore the exact old file and its previous loaded/unloaded state."""
    failures = []
    already_restored, current = False, None
    try:
        current = _command(["/bin/launchctl", "print", target])
        already_restored = (
            was_loaded and not current.returncode and loaded_arguments(current) == old_arguments
        )
    except (OSError, ValueError, subprocess.SubprocessError, RelayInstallError):
        failures.append("回退服务状态暂不可读")
    try:
        # This target was ownership-checked before migration. If its state is
        # unknown, a best-effort stop is still confined to that same service.
        if (current is None or not current.returncode) and not already_restored:
            if _command(["/bin/launchctl", "bootout", target]).returncode:
                failures.append("无法停止新服务")
    except (OSError, subprocess.SubprocessError):
        failures.append("无法停止新服务")
    # Keep each disk restoration independent from launchctl and from the other
    # file. A service status timeout must not leave the new plist installed.
    try:
        _write_bytes(plist, original, mode)
    except (OSError, RelayInstallError):
        failures.append("原连接服务文件未恢复")
    try:
        if old_route is None:
            route.unlink(missing_ok=True)
        else:
            _write_bytes(route, old_route)
    except (OSError, RelayInstallError):
        failures.append("原路线文件未恢复")
    if was_loaded and not already_restored:
        try:
            running_document = plistlib.loads(original)
            running_document["ProgramArguments"] = old_arguments
            _write_bytes(plist, plistlib.dumps(running_document), mode)
            if _command(["/bin/launchctl", "bootstrap", domain, str(plist)]).returncode:
                failures.append("旧服务未恢复运行")
            else:
                observed = loaded_arguments(_command(["/bin/launchctl", "print", target]))
                if observed != old_arguments:
                    failures.append("旧服务运行参数未确认")
        except (OSError, ValueError, subprocess.SubprocessError, RelayInstallError):
            failures.append("旧服务恢复检查未完成")
        finally:
            try:
                _write_bytes(plist, original, mode)
            except (OSError, RelayInstallError):
                failures.append("原连接服务文件未恢复")
    return failures


def upgrade_relay(project: Path, device_id="lab01") -> dict:
    """Add dynamic route selection, with exclusive ownership and rollback.

    This operation requires the existing project setup. It deliberately never
    creates another service, picks another device, mutates a whitelist or starts
    a collector. Its backup is retained for the laboratory to inspect.
    """
    project = Path(project).resolve()
    settings = load_settings(project / "config.local.toml")
    manager = ConnectionManager(settings, device_id, project=project)
    service = manager.adb.get("launchd_service", "")
    if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", service):
        raise RelayInstallError("没有可迁移的本项目连接服务。")
    plist = Path.home() / "Library/LaunchAgents" / f"{service}.plist"
    domain, target = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{service}"
    with DeviceLock(manager.serial, settings.state_dir):
        if plist.is_symlink() or not plist.is_file() or plist.stat().st_uid != os.getuid():
            raise RelayInstallError("连接服务不是当前用户拥有的普通文件，未修改。")
        original, mode = plist.read_bytes(), plist.stat().st_mode & 0o777
        try:
            document = plistlib.loads(original)
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise RelayInstallError("连接服务文件无法读取，未修改。") from exc
        if not isinstance(document, dict):
            raise RelayInstallError("连接服务文件格式无效。")
        old_arguments, interface = _validate_owned(project, manager, document)
        updated = list(old_arguments)
        if "--route-config" not in updated:
            updated.extend(["--route-config", str(manager.route_path)])
        old_route = None
        if manager.route_path.exists() or manager.route_path.is_symlink():
            route = manager.route_path
            if (route.is_symlink() or not route.is_file() or route.stat().st_uid != os.getuid()
                    or route.stat().st_mode & 0o077):
                raise RelayInstallError("路线配置不是当前用户的私有文件，未修改。")
            old_route = route.read_bytes()
            try:
                route_data = json.loads(old_route)
            except ValueError as exc:
                raise RelayInstallError("已有路线配置无效，未覆盖。") from exc
            if (not isinstance(route_data, dict) or set(route_data) != {"interface"}
                    or not ConnectionManager._valid_route(route_data["interface"])):
                raise RelayInstallError("已有路线配置无效，未覆盖。")
        current = _command(["/bin/launchctl", "print", target])
        was_loaded = current.returncode == 0
        observed = loaded_arguments(current)
        # A running service with another target must never be replaced merely
        # because its on-disk plist now points at this project.
        legacy = list(updated)
        route_index = legacy.index("--route-config")
        del legacy[route_index:route_index + 2]
        if observed is not None and observed not in [old_arguments, updated, legacy]:
            raise RelayInstallError("已加载的服务与本项目配置不同，未停止该服务。")
        if updated == old_arguments and observed == updated and old_route is not None:
            return {"changed": False, "service": service, "loaded": True,
                    "route_config": str(manager.route_path), "backup_dir": None}
        backups = settings.state_dir / "connection/relay-backups"
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        if backups.is_symlink():
            raise RelayInstallError("连接备份目录不能使用符号链接。")
        backups.chmod(0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=backups))
        _write_bytes(backup / "relay.plist", original)
        if old_route is not None:
            _write_bytes(backup / "route.json", old_route)
        atomic_private_json(backup / "snapshot.json", {
            "service": service, "was_loaded": was_loaded,
            "route_existed": old_route is not None, "plist_mode": mode,
        })
        try:
            if was_loaded and observed != updated:
                if _command(["/bin/launchctl", "bootout", target]).returncode:
                    raise RelayInstallError("连接服务未能停止，迁移已取消。")
            if old_route is None:
                atomic_private_json(manager.route_path, {"interface": interface})
            document["ProgramArguments"] = updated
            _write_bytes(plist, plistlib.dumps(document))
            if not was_loaded or observed != updated:
                if _command(["/bin/launchctl", "bootstrap", domain, str(plist)]).returncode:
                    raise RelayInstallError("新的连接服务未能启动。")
            actual = loaded_arguments(_command(["/bin/launchctl", "print", target]))
            if actual != updated:
                raise RelayInstallError("连接服务没有加载新的路线参数。")
        except (OSError, ValueError, subprocess.SubprocessError, RelayInstallError) as exc:
            failures = _restore(plist, original, mode, manager.route_path, old_route,
                                domain, target, was_loaded, observed or old_arguments)
            if failures:
                raise RelayInstallError(
                    f"连接服务升级失败，回退需要检查；备份：{backup}",
                ) from exc
            raise RelayInstallError("连接服务升级未完成，已恢复原有配置。") from exc
        return {"changed": True, "service": service, "loaded": True,
                "route_config": str(manager.route_path), "backup_dir": str(backup)}
