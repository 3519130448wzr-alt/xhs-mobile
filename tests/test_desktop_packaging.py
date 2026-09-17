"""Synthetic bundle publication checks; never installs an App or touches real files."""

import importlib.util
import plistlib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "desktop_packaging", Path(__file__).resolve().parents[1] / "scripts/build_desktop_app.py"
)
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def bundle(path, bundle_id=packaging.BUNDLE_ID):
    (path / "Contents").mkdir(parents=True)
    with (path / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, stream)
    return path


def test_publication_never_replaces_unrelated_app_or_symlink(tmp_path):
    staged = bundle(tmp_path / "staged.app")
    unrelated = bundle(tmp_path / "unrelated.app", "synthetic.someone.else")
    with pytest.raises(RuntimeError, match="未覆盖"):
        packaging.publish(staged, unrelated)
    alias = tmp_path / "alias.app"
    alias.symlink_to(unrelated)
    with pytest.raises(RuntimeError, match="未覆盖"):
        packaging.publish(staged, alias)
    assert staged.is_dir() and alias.is_symlink() and unrelated.is_dir()


def test_failed_replacement_restores_previous_app(tmp_path, monkeypatch):
    destination = bundle(tmp_path / "installed.app")
    (destination / "synthetic-sentinel").write_text("previous")
    staged = bundle(tmp_path / "staged.app")
    original = Path.rename

    def fail_staged(self, target):
        if self == staged:
            raise OSError("SYNTHETIC installation failure")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", fail_staged)
    with pytest.raises(OSError, match="SYNTHETIC"):
        packaging.publish(staged, destination)
    assert (destination / "synthetic-sentinel").read_text() == "previous"
    assert not destination.with_name(destination.name + ".previous").exists()


def test_existing_rollback_is_not_destroyed(tmp_path):
    destination = bundle(tmp_path / "installed.app")
    staged = bundle(tmp_path / "staged.app")
    backup = bundle(tmp_path / "installed.app.previous")
    with pytest.raises(RuntimeError, match="备份"):
        packaging.publish(staged, destination)
    assert destination.is_dir() and staged.is_dir() and backup.is_dir()
