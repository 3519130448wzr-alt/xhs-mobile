"""Optional desktop parent-liveness guard; ordinary terminal commands are unchanged.

The desktop controller passes a read-only pipe with ``pass_fds`` and keeps its
write end open. EOF or *any* byte asks the existing runner to pause at its next
safe boundary. No phone command is killed. This context must enclose command
setup as well as execution, rather than starting only after a run is created.
"""

from __future__ import annotations

import fcntl
import os
import select
import stat
import threading
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps

CONTROL_FD_ENV = "XHS_DESKTOP_CONTROL_FD"


class DesktopParentGone(ValueError):
    """The desktop controller stopped before execution could safely begin."""


class DesktopControl:
    def __init__(self, value: str | None):
        self.stop = threading.Event()
        self._closed = threading.Event()
        self._read_lock = threading.Lock()
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        if value is None:
            return
        # Never read stdin or close a descriptor merely because an environment
        # variable names it. Validate the inherited pipe before duplicating it.
        if not value.isascii() or not value.isdecimal() or int(value) < 3:
            raise ValueError("Invalid desktop control pipe descriptor")
        source = int(value)
        try:
            metadata = os.fstat(source)
            mode = fcntl.fcntl(source, fcntl.F_GETFL) & os.O_ACCMODE
            if not stat.S_ISFIFO(metadata.st_mode) or mode != os.O_RDONLY:
                raise ValueError("Desktop control descriptor must be a read-only pipe")
            self._fd = os.dup(source)
            os.set_inheritable(self._fd, False)
        except (OSError, OverflowError) as exc:
            raise ValueError("Desktop control pipe is unavailable") from exc

    def __enter__(self):
        if self._fd is not None:
            # Catch a parent which died before the child reached this context,
            # without relying on scheduling the reader thread first.
            self.poll()
            self._thread = threading.Thread(
                target=self._watch, name="xhs-desktop-control", daemon=True,
            )
            self._thread.start()
        return self

    def poll(self) -> None:
        with self._read_lock:
            if self._fd is None or self.stop.is_set() or self._closed.is_set():
                return
            try:
                ready, _, _ = select.select([self._fd], [], [], 0)
                if ready:
                    # Both a byte and EOF make a pipe readable. They have the
                    # same meaning, so do not perform a potentially blocking
                    # read or consume data belonging to a caller's descriptor.
                    self.stop.set()
            except (OSError, ValueError):
                # Losing the safety channel is itself a stop request.
                self.stop.set()

    def _watch(self) -> None:
        while not self._closed.wait(0.1):
            self.poll()
            if self.stop.is_set():
                return

    def stopped(self) -> bool:
        self.poll()
        return self.stop.is_set()

    def check_start(self) -> None:
        if self.stopped():
            raise DesktopParentGone("桌面控制程序已断开或请求暂停；尚未开始新的设备操作。")

    def __exit__(self, *_):
        self._closed.set()
        if self._thread is not None:
            self._thread.join()
        with self._read_lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


_current: ContextVar[DesktopControl | None] = ContextVar("desktop_control", default=None)


def desktop_controlled(function):
    """Start the optional watcher before configuration, task creation or I/O."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with DesktopControl(os.environ.get(CONTROL_FD_ENV)) as control:
            token = _current.set(control)
            try:
                control.check_start()
                return function(*args, **kwargs)
            finally:
                _current.reset(token)

    return wrapper


def check_desktop_parent() -> None:
    """Reject new durable work after early setup if the controller has gone."""
    control = _current.get()
    if control is not None:
        control.check_start()


def execution_stop() -> tuple[threading.Event, Callable[[], bool]]:
    """Share one stop event across setup, signal handlers and all batch children."""
    control = _current.get()
    if control is not None:
        return control.stop, control.stopped
    event = threading.Event()
    return event, event.is_set
