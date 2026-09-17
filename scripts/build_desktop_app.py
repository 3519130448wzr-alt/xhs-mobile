"""Build and optionally install the native launcher for this configured Mac.

The app intentionally reuses this checkout's Python environment and private config.
Never package credentials, evidence, database data, or the virtual environment.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
APP_NAME = "小红书采集助手.app"
BUNDLE_ID = "edu.cityu.mobilelab.xhsassistant"


def command(arguments: list[str], *, cwd: Path = PROJECT) -> str:
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "构建命令失败")
    return result.stdout.strip()


def owned_app(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        with (path / "Contents" / "Info.plist").open("rb") as stream:
            return plistlib.load(stream).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def publish(staged: Path, destination: Path):
    """Replace only this app's bundle; retain a rollback copy until replacement succeeds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if not owned_app(destination):
            raise RuntimeError(f"目标不是本项目的 App，未覆盖：{destination}")
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists() or backup.is_symlink():
        raise RuntimeError(f"存在上次安装备份，请先检查：{backup}")
    had_previous = destination.exists()
    if had_previous:
        destination.rename(backup)
    try:
        staged.rename(destination)
    except OSError:
        if had_previous:
            backup.rename(destination)
        raise
    if had_previous:
        shutil.rmtree(backup)


def build(project: Path, *, install: bool, device: str = "lab01") -> Path:
    project = project.resolve()
    if sys.platform != "darwin":
        raise RuntimeError("原生桌面 App 需要在 macOS 上构建")
    python = project / ".venv" / "bin" / "python"
    bridge = project / "scripts" / "desktop_bridge.py"
    for dependency in (python, bridge, project / "desktop" / "Package.swift"):
        if not dependency.exists():
            raise RuntimeError(f"缺少构建依赖：{dependency}")
    build_args = ["xcrun", "swift", "build", "--package-path", str(project / "desktop"),
                  "--scratch-path", str(project / ".tools" / "desktop-build"),
                  "-c", "release"]
    command(build_args, cwd=project)
    binaries = Path(command([*build_args, "--show-bin-path"], cwd=project))
    destination = ((Path.home() / "Applications") if install else (project / "dist")) / APP_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xhs-app-build-", dir=destination.parent) as temp:
        root = Path(temp)
        app = root / APP_NAME
        contents = app / "Contents"
        resources = contents / "Resources"
        executable_dir = contents / "MacOS"
        resources.mkdir(parents=True)
        executable_dir.mkdir()
        executable = executable_dir / "XHSMobileDesktop"
        shutil.copy2(binaries / "XHSMobileDesktop", executable)
        executable.chmod(0o755)
        config_path = resources / "desktop-config.json"
        with (project / "config.local.toml").open("rb") as stream:
            settings = tomllib.load(stream)
        state_dir = Path(settings.get("state_dir", "var"))
        if not state_dir.is_absolute():
            state_dir = project / state_dir
        config_path.write_text(json.dumps({
            "project_path": str(project), "python_path": str(python),
            "bridge_path": str(bridge), "device_id": device,
            "connection_record_path": str(state_dir / "connection" / f"mobile-{device}.json"),
        }, ensure_ascii=False, indent=2) + "\n")
        config_path.chmod(0o600)
        image = root / "icon.png"
        command(["xcrun", "swift", str(project / "scripts" / "app_icon.swift"), str(image)])
        iconset = root / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                command(["sips", "-z", str(size * scale), str(size * scale), str(image),
                         "--out", str(iconset / f"icon_{size}x{size}{suffix}.png")])
        command(["iconutil", "-c", "icns", str(iconset),
                 "-o", str(resources / "AppIcon.icns")])
        info = {
            "CFBundleName": "小红书采集助手", "CFBundleDisplayName": "小红书采集助手",
            "CFBundleExecutable": executable.name, "CFBundleIdentifier": BUNDLE_ID,
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.5.0",
            "CFBundleVersion": "7", "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "LSMultipleInstancesProhibited": True,
            "NSDocumentsFolderUsageDescription":
                "读取手机采集项目的运行环境、配置、已保存记录及来源证据。",
            "NSHumanReadableCopyright": "CityU Mobile Lab · 本机实验采集工具",
        }
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(info, stream)
        command(["codesign", "--force", "--sign", "-", str(app)])
        command(["codesign", "--verify", "--deep", "--strict", str(app)])
        publish(app, destination)
    if install:
        from install_connection_relay import upgrade_relay

        upgrade_relay(project, device_id=device)
        shortcut = Path.home() / "Desktop" / APP_NAME
        if shortcut.is_symlink() and shortcut.resolve() == destination.resolve():
            pass
        elif shortcut.exists() or shortcut.is_symlink():
            raise RuntimeError(f"App 已安装，但桌面存在同名文件，未覆盖：{shortcut}")
        else:
            shortcut.symlink_to(destination)
        command(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                 "LaunchServices.framework/Support/lsregister", "-f", str(destination)])
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建小红书采集助手；--install 安装到本机并创建桌面入口"
    )
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--device", default="lab01")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(build(args.project, install=args.install, device=args.device))
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
