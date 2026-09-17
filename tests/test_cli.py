"""CLI smoke tests deliberately never connect to an Android device."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from xhs_mobile import cli as module
from xhs_mobile.cli import app
from xhs_mobile.config import Settings, load_settings

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def test_help_and_version_do_not_require_configuration():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "snapshot", "collect-current", "resume", "export", "acceptance"):
        assert command in result.stdout
    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0
    assert "0.5.0" in version.stdout


def test_missing_configuration_has_actionable_error(tmp_path):
    result = runner.invoke(
        app, ["--config", str(tmp_path / "missing.toml"), "doctor", "--device", "lab01"]
    )
    assert result.exit_code == 2
    assert "config.example.toml" in result.stdout


def test_doctor_reports_missing_serial_before_device_contact(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text((ROOT / "config.example.toml").read_text())
    monkeypatch.delenv("XHS_ADB_SERIAL", raising=False)
    result = runner.invoke(app, ["--config", str(path), "doctor", "--device", "lab01"])
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert "XHS_ADB_SERIAL" in report["configuration_error"]
    assert report["dependencies"]["device_contacted"] is False


def test_uncalibrated_profile_fails_without_database_or_device():
    result = runner.invoke(
        app, ["profile-check", "--profile", str(ROOT / "profiles/template.toml")]
    )
    assert result.exit_code == 2
    assert "verified" in result.stdout


def test_run_rejects_template_before_opening_connections(tmp_path, monkeypatch):
    content = (
        (ROOT / "config.example.toml")
        .read_text()
        .replace(
            '"profiles/template.toml"',
            json.dumps(str(ROOT / "profiles/template.toml")),
        )
    )
    path = tmp_path / "local.toml"
    path.write_text(content)
    monkeypatch.delenv("XHS_DATABASE_URL", raising=False)
    monkeypatch.delenv("XHS_ADB_SERIAL", raising=False)
    result = runner.invoke(
        app, ["--config", str(path), "run", "--device", "lab01", "--keyword", "TEST_ONLY"]
    )
    assert result.exit_code == 2
    assert "verified" in result.stdout


def test_settings_resolve_paths_relative_to_configuration(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text((ROOT / "config.example.toml").read_text())
    loaded = load_settings(path)
    assert loaded.state_dir == tmp_path / "var"
    assert loaded.profile_path == tmp_path / "profiles/template.toml"


def test_production_configuration_rejects_sqlite(monkeypatch):
    monkeypatch.setenv("XHS_DATABASE_URL", "sqlite:///fake.db")
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings().database_url()


@pytest.mark.parametrize("text", ["unknown = true", "[policy]\nmax_retries = -1"])
def test_invalid_configuration_rejected(tmp_path, text):
    path = tmp_path / "local.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_settings(path)


@pytest.mark.parametrize("prefix", ["", "搜索, "])
def test_doctor_requires_explicit_input_test_for_prefix(tmp_path, monkeypatch, prefix):
    adapter = Mock(side_effect=AssertionError("Must validate options before device contact"))
    monkeypatch.setattr(module, "device_for", adapter)
    result = runner.invoke(app, [
        "--config", str(tmp_path / "not-created.toml"), "doctor", "--device", "lab01",
        "--input-prefix", prefix,
    ])
    assert result.exit_code == 2
    assert "--input-prefix requires --check-input" in result.stdout
    adapter.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "搜索, "])
def test_doctor_forwards_prefix_without_typing_it(tmp_path, monkeypatch, prefix):
    path = tmp_path / "local.toml"
    path.write_text((ROOT / "config.example.toml").read_text())
    monkeypatch.setenv("XHS_ADB_SERIAL", "synthetic")
    phone = Mock()
    phone.health.return_value = {"ok": True}
    phone.check_input.return_value = {"ok": True, "readback_prefix": prefix}
    monkeypatch.setattr(module, "device_for", Mock(return_value=phone))
    monkeypatch.setattr(module, "dependency_report", Mock(return_value={"device_contacted": False}))
    monkeypatch.setattr(module, "adb_devices", Mock(return_value=[]))
    arguments = ["--config", str(path), "doctor", "--device", "lab01", "--check-input"]
    if prefix:
        arguments += ["--input-prefix", prefix]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["input_check"]["readback_prefix"] == prefix
    phone.check_input.assert_called_once_with(readback_prefix=prefix)
