"""Single-controller-host device exclusion backed by OS advisory locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from .domain import DeviceBusy, DeviceError


class DeviceLock:
    """All controllers must use the same state_dir on this host.

    Lock files are deliberately retained: unlinking an unlocked file could let
    another controller lock a different inode for the same device. The kernel
    releases flock automatically after a crash or a process exit.
    """

    def __init__(self, serial: str, state_dir: str | Path) -> None:
        if not serial or not serial.strip():
            raise ValueError("Device lock requires an explicit serial")
        self.serial = serial
        self.directory = Path(state_dir) / "locks"
        self.path = self.directory / f"{hashlib.sha256(serial.encode()).hexdigest()}.lock"
        self._fd: int | None = None

    def __enter__(self) -> DeviceLock:
        if self._fd is not None:
            raise DeviceBusy("This lock instance already owns the device")
        fd = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.is_symlink() or self.path.is_symlink():
                raise DeviceError("Device lock paths must not be symbolic links")
            self.directory.chmod(0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeviceBusy("Another process already controls this device") from exc
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid()}).encode())
            self._fd = fd
            fd = None
            return self
        except OSError as exc:
            raise DeviceError(f"Cannot acquire device lock: {exc.strerror}") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
