"""Explicit-serial Android driver with bounded external operations.

uiautomator2 is loaded in a short-lived subprocess. Its connection setup and
fallbacks can contain their own waits; a subprocess deadline bounds the entire
operation and prevents timed-out Python threads from continuing to tap a phone.
No connection or automation-service setup occurs during construction.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any

from .domain import DeviceUnavailable, Snapshot, UIState, utcnow


def dependency_report(adb_path: str = "adb") -> dict[str, Any]:
    """Inspect local dependencies without contacting ADB or a device."""
    modules: dict[str, dict[str, Any]] = {}
    for distribution, module in (("uiautomator2", "uiautomator2"), ("Pillow", "PIL")):
        found = importlib.util.find_spec(module) is not None
        try:
            version = importlib.metadata.version(distribution) if found else None
        except importlib.metadata.PackageNotFoundError:
            version = None
        modules[module] = {"available": found, "version": version}
    return {
        "python": sys.version.split()[0],
        "adb": shutil.which(adb_path),
        "tesseract": shutil.which("tesseract"),
        "modules": modules,
        "device_contacted": False,
    }


def _execute(argv: list[str], timeout: float, *, input_text: str | None = None) -> bytes:
    try:
        # Desktop stdin belongs exclusively to the JSONL controller. In
        # particular, adb shell must not consume pending pause/status requests.
        input_options = (
            {"input": input_text.encode("utf-8")}
            if input_text is not None else {"stdin": subprocess.DEVNULL}
        )
        completed = subprocess.run(
            argv,
            **input_options,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeviceUnavailable(f"Device operation exceeded {timeout:g} seconds") from exc
    except OSError as exc:
        raise DeviceUnavailable(f"Cannot execute {argv[0]}: {exc.strerror}") from exc
    if completed.returncode:
        # Never echo stdin (which can contain a search term) or an entire command.
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise DeviceUnavailable(f"Device command failed ({completed.returncode}): {detail}")
    return completed.stdout


def adb_devices(adb_path: str = "adb", timeout: float = 30) -> list[dict[str, str]]:
    """List ADB transports; never choose the first device implicitly."""
    output = _execute([adb_path, "devices", "-l"], timeout).decode("utf-8", errors="replace")
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1], "details": " ".join(parts[2:])})
    return devices


def _package(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", value):
        raise ValueError("An explicit Android package name is required")
    return value


class AndroidDevice:
    def __init__(
        self, serial: str, package: str, adb_path: str = "adb", timeout: float = 30
    ) -> None:
        if not serial or serial.startswith("-") or any(c.isspace() for c in serial):
            raise ValueError("An explicit, nonempty ADB serial without whitespace is required")
        if timeout <= 0:
            raise ValueError("Device timeout must be positive")
        self.serial = serial
        self.package = _package(package) if package else ""
        self.adb_path = adb_path
        self.timeout = timeout
        self._version_cache: dict[str, str | None] = {}

    def _adb(self, *args: str) -> bytes:
        return _execute([self.adb_path, "-s", self.serial, *args], self.timeout)

    def _shell(self, *args: str) -> str:
        return self._adb("shell", *args).decode("utf-8", errors="replace").strip()

    def _u2(self, operation: str, **kwargs: Any) -> Any:
        request = {
            "serial": self.serial, "operation": operation, "timeout": self.timeout,
            "adb_path": self.adb_path, **kwargs,
        }
        raw = _execute(
            [sys.executable, "-m", "xhs_mobile.device", "--uiautomator-worker"],
            self.timeout,
            input_text=json.dumps(request, ensure_ascii=False),
        )
        try:
            response = json.loads(raw)
            if response.get("ok") is not True:
                raise DeviceUnavailable(str(response.get("error", "Automation service failed")))
            return response["result"]
        except (ValueError, KeyError, AttributeError, TypeError) as exc:
            raise DeviceUnavailable("Automation worker returned an invalid response") from exc

    def _version(self, package: str | None = None, *, refresh: bool = False) -> str | None:
        package = self.package if package is None else package
        if not package:
            return None
        # Framework-owned foreground windows may use the single-segment package
        # "android". Metadata inspection permits these safe identifier tokens;
        # launching an application still requires an explicit application ID.
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", package):
            raise DeviceUnavailable("Cannot validate foreground package for version inspection")
        if not refresh and package in self._version_cache:
            return self._version_cache[package]
        output = self._shell("dumpsys", "package", package)
        match = re.search(r"\bversionName=([^\s]+)", output)
        version = match.group(1) if match else None
        self._version_cache[package] = version
        return version

    def _foreground(self) -> dict[str, str | None]:
        output = self._shell("dumpsys", "activity", "activities")
        for line in output.splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line:
                match = re.search(r"\b([A-Za-z][\w.]*)/([A-Za-z0-9_.$]+)", line)
                if match:
                    return {"package": match.group(1), "activity": match.group(2)}
        return {"package": None, "activity": None}

    def health(self) -> dict[str, Any]:
        """Probe the configured phone; errors are returned for a diagnostic report.

        The automation probe may install/start uiautomator2's helper service. It
        never grants app permissions, logs in, changes accounts, or types text.
        """
        report: dict[str, Any] = {
            "ok": False,
            "serial": self.serial,
            "configured_package": self.package,
            "needs_package_configuration": not bool(self.package),
            "package_candidates": [],
            "checked_at": utcnow().isoformat(),
            "checks": {},
            "errors": [],
            "chinese_input": "not_tested_requires_lab_focused_input_and_explicit_input_test",
        }
        checks = report["checks"]
        try:
            state = self._adb("get-state").decode().strip()
            checks["adb_state"] = state
            if state != "device":
                raise DeviceUnavailable(f"ADB transport state is {state!r}")
            checks["android_version"] = self._shell("getprop", "ro.build.version.release")
            checks["android_sdk"] = self._shell("getprop", "ro.build.version.sdk")
            if self.package:
                path = self._shell("pm", "path", self.package)
                checks["package_installed"] = any(
                    line.startswith("package:") for line in path.splitlines()
                )
                if not checks["package_installed"]:
                    raise DeviceUnavailable("Configured Android package is not installed")
                checks["app_version"] = self._version(refresh=True)
            else:
                packages = self._shell("pm", "list", "packages", "-3")
                report["package_candidates"] = sorted(
                    line.removeprefix("package:").strip()
                    for line in packages.splitlines() if line.startswith("package:")
                )
                checks["package_installed"] = checks["app_version"] = None
            checks["foreground"] = self._foreground()
            checks["automation_probe"] = {
                "helper_may_be_started": True,
                "result": self._u2("info"),
            }
            report["ok"] = bool(self.package)
        except DeviceUnavailable as exc:
            report["errors"].append(str(exc))
        return report

    def read_state(self) -> UIState:
        """Read navigation state without screenshots or content supplements.

        This deliberately cannot be submitted as full source evidence. Window
        size and foreground are checked each time; only app versions are cached
        within this driver instance and refreshed by an explicit health probe.
        """
        started = time.monotonic()
        result = self._u2("navigation_state")
        if not isinstance(result, dict) or not isinstance(result.get("xml"), str):
            raise DeviceUnavailable("Navigation worker returned invalid UI state")
        if not result["xml"].strip():
            raise DeviceUnavailable("UI hierarchy is empty")
        size = result.get("resolution")
        if (not isinstance(size, list) or len(size) != 2
                or any(type(v) is not int or v <= 0 for v in size)):
            raise DeviceUnavailable("Navigation worker returned invalid display dimensions")
        hierarchy_seconds = time.monotonic() - started
        foreground = self._foreground()
        version = self._version(foreground["package"] or self.package)
        return UIState(xml=result["xml"], metadata={
            "source_kind": "android", "read_kind": "navigation", "serial": self.serial,
            "configured_package": self.package, **foreground,
            "app_package": foreground["package"], "app_version": version,
            "resolution": size,
            "capture_timings": {
                "hierarchy_seconds": round(hierarchy_seconds, 3),
                "metadata_seconds": round(time.monotonic() - started - hierarchy_seconds, 3),
                "screenshot_transfer_seconds": 0,
                "total_seconds": round(time.monotonic() - started, 3),
            },
        })

    def set_clipboard(self, text: str) -> None:
        """Only the explicitly bound Android clipboard, never the Mac clipboard."""
        self._u2("set_clipboard", text=text)

    def get_clipboard(self) -> str:
        result = self._u2("get_clipboard")
        if not isinstance(result, str):
            raise DeviceUnavailable("Android clipboard returned a non-text value")
        return result

    def capture(self) -> Snapshot:
        started_at = utcnow()
        started = time.monotonic()
        timings: dict[str, float] = {}
        from .ui_readback import unavailable_readbacks

        try:
            captured = self._u2("capture_hierarchy", configured_package=self.package)
        except DeviceUnavailable:
            # A killed/failed supplement must not prevent a plain diagnostic.
            # This is a new bounded hierarchy read, not the lost worker's frame.
            xml = self._u2("hierarchy")
            readbacks = unavailable_readbacks(xml, self.package, "capture_worker_unavailable")
        else:
            if isinstance(captured, dict) and isinstance(captured.get("readbacks"), dict):
                xml = captured.get("xml")
                readbacks = captured["readbacks"]
            elif isinstance(captured, str):
                # Compatibility for older worker/test doubles: no supplement is trusted.
                xml = captured
                readbacks = unavailable_readbacks(xml, self.package, "legacy_hierarchy_only")
            else:
                raise DeviceUnavailable("Automation worker returned invalid hierarchy evidence")
        if not isinstance(xml, str) or not xml.strip():
            raise DeviceUnavailable("UI hierarchy is empty")
        timings["hierarchy_seconds"] = round(time.monotonic() - started, 3)
        stage_started = time.monotonic()
        png = self._adb("exec-out", "screencap", "-p")
        timings["screenshot_transfer_seconds"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
            raise DeviceUnavailable("Android screenshot is not PNG data")
        # Pillow's PNG verifier does not require the final IEND CRC on every
        # version. Require the complete terminal chunk before decoding, so even
        # a response truncated by one byte is rejected as incomplete evidence.
        if not png.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise DeviceUnavailable("Android screenshot is truncated or missing its PNG end chunk")
        try:
            from PIL import Image
        except ImportError as exc:
            raise DeviceUnavailable("Pillow is required to verify screenshot integrity") from exc
        try:
            with Image.open(io.BytesIO(png)) as picture:
                if picture.format != "PNG":
                    raise ValueError("Screenshot format is not PNG")
                width, height = picture.size
                picture.verify()
            # Verification checks PNG structure/checksums; decoding also catches
            # malformed compressed pixel data without changing the saved bytes.
            with Image.open(io.BytesIO(png)) as picture:
                picture.load()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise DeviceUnavailable("Android screenshot failed PNG integrity verification") from exc
        timings["screenshot_verify_seconds"] = round(time.monotonic() - stage_started, 3)
        stage_started = time.monotonic()
        foreground = self._foreground()
        version = self._version(foreground["package"] or self.package)
        timings["metadata_seconds"] = round(time.monotonic() - stage_started, 3)
        timings["total_seconds"] = round(time.monotonic() - started, 3)
        readbacks["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
        metadata = {
            "source_kind": "android",
            "serial": self.serial,
            "configured_package": self.package,
            **foreground,
            "app_package": foreground["package"],
            "app_version": version,
            "resolution": [width, height],
            "capture_started_at": started_at.isoformat(),
            "capture_timings": timings,
            "capture_sequence": "hierarchy_optional_objinfo_recheck_then_screenshot_then_metadata",
            "ui_readbacks": readbacks,
        }
        # Captures are sequential evidence, not a claim of frame-atomic UI state.
        return Snapshot(xml=xml, png=png, metadata=metadata)

    def check_input(
        self, text: str = "中文输入测试", *, readback_prefix: str = ""
    ) -> dict[str, Any]:
        """Explicit opt-in diagnostic; requires a focused non-password input.

        The caller/laboratory must first focus an expendable search field. This
        replaces its current contents, verifies exact UI readback, and never
        presses Enter or submits a search. The text is deliberately left visible.
        A calibrated accessibility prefix is part of the expected display value;
        it is never typed, stripped, or matched as a substring.
        """
        report: dict[str, Any] = {
            "ok": False, "input_attempted": False, "submitted": False,
            "readback_prefix": readback_prefix,
        }
        if not isinstance(text, str) or not text:
            raise ValueError("Input diagnostic text must be nonempty")
        if not isinstance(readback_prefix, str):
            raise ValueError("Input diagnostic readback prefix must be a string")

        def focused_input(xml: str):
            root = ET.fromstring(xml)
            candidates = []
            pending = [(root, ())]
            while pending:
                node, path = pending.pop()
                if node.get("visible-to-user") == "false":
                    continue
                editable = "EditText" in node.get("class", "") or node.get("editable") == "true"
                if node.tag == "node" and node.get("focused") == "true" and editable:
                    # Top-level Android windows can be reordered when the IME
                    # updates. Only the path inside that window identifies the
                    # field; its own package/class/ID/bounds must still agree.
                    window_path = path[1:] if root.tag == "hierarchy" else path
                    identity = (window_path, *(node.get(key) for key in (
                        "package", "class", "resource-id", "bounds"
                    )))
                    candidates.append((node, identity))
                pending.extend((child, (*path, index)) for index, child in enumerate(node))
            if len(candidates) != 1 or candidates[0][0].get("password") == "true":
                return None
            return candidates[0]

        try:
            before = focused_input(self._u2("hierarchy"))
            if before is None:
                report["reason"] = (
                    "Focus one unique non-password search input before running this diagnostic"
                )
                return report
            report["input_attempted"] = True
            self.input_text(text)
            after = focused_input(self._u2("hierarchy"))
            report["ok"] = (
                after is not None
                and after[1] == before[1]
                and after[0].get("text") == readback_prefix + text
            )
            report["reason"] = (
                "exact_ui_readback" if report["ok"] else "input_not_readable_or_mismatch"
            )
        except (DeviceUnavailable, ET.ParseError) as exc:
            report["reason"] = str(exc)
        return report

    def start_app(self, package: str) -> None:
        output = self._shell(
            "monkey", "-p", _package(package), "-c", "android.intent.category.LAUNCHER", "1"
        )
        if "No activities found" in output or "monkey aborted" in output.lower():
            raise DeviceUnavailable("Configured App has no launchable activity")

    def stop_app(self, package: str) -> None:
        self._shell("am", "force-stop", _package(package))

    def click(self, bounds: tuple[int, int, int, int]) -> None:
        if len(bounds) != 4 or any(not isinstance(x, int) for x in bounds):
            raise ValueError("Click bounds must contain four integer coordinates")
        left, top, right, bottom = bounds
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("Click bounds must be a positive rectangle")
        self._shell("input", "tap", str((left + right) // 2), str((top + bottom) // 2))

    def input_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise ValueError("Input text must be a string")
        self._u2("input_text", text=text)

    def press(self, key: str) -> None:
        keys = {"back": "4", "home": "3", "enter": "66", "search": "84", "delete": "67"}
        if key not in keys:
            raise ValueError(f"Unsupported key: {key}")
        self._shell("input", "keyevent", keys[key])

    def swipe(self, direction: str = "up") -> None:
        if direction not in {"up", "down", "left", "right"}:
            raise ValueError(f"Unsupported swipe direction: {direction}")
        sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", self._shell("wm", "size"))
        if not sizes:
            raise DeviceUnavailable("Cannot determine device display dimensions")
        width, height = map(int, sizes[-1])
        if width <= 0 or height <= 0:
            raise DeviceUnavailable("Device display has invalid dimensions")
        points = {
            "up": (0.5, 0.75, 0.5, 0.3),
            "down": (0.5, 0.3, 0.5, 0.75),
            "left": (0.75, 0.5, 0.25, 0.5),
            "right": (0.25, 0.5, 0.75, 0.5),
        }[direction]
        coords = [str(int(p * (width if i % 2 == 0 else height))) for i, p in enumerate(points)]
        self._shell("input", "swipe", *coords, "500")


def _uiautomator_worker() -> None:
    """Private subprocess entry point, deliberately not a public CLI command."""
    try:
        request = json.load(sys.stdin)
        serial = request["serial"]
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("Explicit serial required")
        # adbutils otherwise resolves a separate PATH/bundled binary when it
        # needs to start the ADB server. Keep it on the driver's configured
        # executable. This environment change is confined to this worker.
        adb_path = request["adb_path"]
        if not isinstance(adb_path, str) or not adb_path:
            raise ValueError("Explicit ADB executable required")
        os.environ["ADBUTILS_ADB_PATH"] = adb_path
        # Third-party startup messages must not corrupt the JSON wire response.
        with contextlib.redirect_stdout(sys.stderr):
            import uiautomator2 as u2

            device = u2.connect(serial)
            device.settings["wait_timeout"] = request["timeout"]
            operation = request["operation"]
            if operation == "hierarchy":
                result = device.dump_hierarchy(compressed=False)
            elif operation == "navigation_state":
                result = {"xml": device.dump_hierarchy(compressed=False),
                          "resolution": list(device.window_size())}
            elif operation == "capture_hierarchy":
                from .ui_readback import capture_hierarchy

                result = capture_hierarchy(device, request["configured_package"])
            elif operation == "info":
                result = device.info
            elif operation == "input_text":
                device.send_keys(request["text"], clear=True)
                result = None
            elif operation == "set_clipboard":
                device.clipboard = request["text"]
                result = None
            elif operation == "get_clipboard":
                result = device.clipboard
            else:
                raise ValueError("Unknown automation worker operation")
        response = {"ok": True, "result": result}
    except Exception as exc:
        # Avoid including supplied input text in potentially sensitive tracebacks.
        response = {"ok": False, "error": f"uiautomator2 operation failed: {type(exc).__name__}"}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--uiautomator-worker"]:
        _uiautomator_worker()
    else:
        raise SystemExit("Use the xhs-mobile command line interface")
