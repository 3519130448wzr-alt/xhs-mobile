#!/usr/bin/env python3
"""Private JSONL bridge used by the native macOS App; stdout is protocol-only."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "scripts"))

from xhs_mobile.desktop import DesktopController  # noqa: E402
from xhs_mobile.desktop_runtime import DesktopRuntime  # noqa: E402
from xhs_mobile.locking import DeviceLock  # noqa: E402


def serve(controller, source, output):
    """The frontend owns stdin. EOF, signal, or broken stdout always safely stops work."""
    stopping = threading.Event()
    write_lock = threading.Lock()

    def emit(value):
        with write_lock:
            try:
                output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                output.flush()
            except (BrokenPipeError, OSError):
                stopping.set()
                controller.runtime.pause()

    controller.emit = emit
    previous = {}
    pending = b""
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, lambda *_: stopping.set())
    try:
        emit({"event": "state", "data": controller.snapshot()})
        while not stopping.is_set():
            state = controller.snapshot()
            if state["shutting_down"] and not state["busy"] and state["activity"] is None:
                break
            if not select.select([source], [], [], 0.2)[0]:
                continue
            chunk = os.read(source.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            if len(pending) > 1_048_576:
                emit({"id": None, "ok": False, "error": {
                    "code": "invalid_request", "message": "请求过长或不完整。",
                }})
                break
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    emit({"id": None, "ok": False, "error": {
                        "code": "invalid_request", "message": "请求格式无效。",
                    }})
                    continue
                emit(controller.request(request))
    finally:
        controller.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description="小红书采集助手桌面控制层")
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--device", default="lab01")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        runtime = DesktopRuntime(args.project, args.device)
        # Separate from physical device/legacy launcher locks; a second App cannot
        # accidentally create a second bridge, while CLI locks still arbitrate the phone.
        with DeviceLock(f"desktop:{args.device}", runtime.settings.state_dir / "desktop"):
            serve(DesktopController(runtime), sys.stdin, sys.stdout)
    except Exception as exc:
        # Raw exceptions can contain private connection settings. Never print them to the UI.
        print(json.dumps({"event": "fatal", "data": {
            "code": "bridge_start", "message": "桌面控制层未能启动，请检查配置或已有窗口。",
            "error_type": type(exc).__name__,
        }}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
