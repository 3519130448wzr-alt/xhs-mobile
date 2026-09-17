"""Real OS lock tests against synthetic serials, without any Android access."""

import os
import subprocess
import sys

import pytest

from xhs_mobile.domain import DeviceBusy, DeviceError
from xhs_mobile.locking import DeviceLock


def test_same_device_is_exclusive_and_lockfile_is_retained(tmp_path):
    first = DeviceLock("synthetic-device", tmp_path)
    with first:
        with pytest.raises(DeviceBusy), DeviceLock("synthetic-device", tmp_path):
            pytest.fail("Second holder must never enter")
    assert first.path.exists()
    with DeviceLock("synthetic-device", tmp_path):
        pass


def test_different_devices_can_be_locked(tmp_path):
    with DeviceLock("synthetic-one", tmp_path), DeviceLock("synthetic-two", tmp_path):
        assert len(list((tmp_path / "locks").iterdir())) == 2


def test_context_exception_releases_lock(tmp_path):
    with pytest.raises(RuntimeError), DeviceLock("synthetic", tmp_path):
        raise RuntimeError("synthetic failure")
    with DeviceLock("synthetic", tmp_path):
        pass


def test_lock_uses_private_hash_path(tmp_path):
    lock = DeviceLock("synthetic-host:5555", tmp_path)
    with lock:
        assert "synthetic" not in lock.path.name
        assert lock.path.stat().st_mode & 0o777 == 0o600
        assert lock.directory.stat().st_mode & 0o777 == 0o700


def test_symlink_lockfile_is_rejected(tmp_path):
    lock = DeviceLock("synthetic", tmp_path)
    lock.directory.mkdir()
    target = tmp_path / "target"
    target.write_text("must survive")
    os.symlink(target, lock.path)
    with pytest.raises(DeviceError, match="symbolic links"), lock:
        pytest.fail("Symlink must be rejected")
    assert target.read_text() == "must survive"


def test_lock_is_exclusive_across_processes_and_released_after_kill(tmp_path):
    code = (
        "import sys,time; from xhs_mobile.locking import DeviceLock; "
        "lock=DeviceLock('synthetic',sys.argv[1]); lock.__enter__(); "
        "print('locked',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(DeviceBusy), DeviceLock("synthetic", tmp_path):
            pytest.fail("Cross-process exclusion failed")
        child.kill()
        child.wait(timeout=5)
        with DeviceLock("synthetic", tmp_path):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
