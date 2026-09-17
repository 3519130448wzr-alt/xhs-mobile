"""SYNTHETIC ownership/credential/backup tests; never contact a PostgreSQL server."""

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "local_postgres.py"
SPEC = importlib.util.spec_from_file_location("_test_local_postgres", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def database(tmp_path):
    root = tmp_path / "var" / "postgres-live"
    root.mkdir(mode=0o700, parents=True)
    (root / "data").mkdir(mode=0o700)
    (root / "data" / "PG_VERSION").write_text("17\n")
    MODULE.write_private(root / "OWNER", MODULE.OWNER + "\n")
    MODULE.write_private(root / "credentials.json", json.dumps({
        "admin_password": "SYNTHETIC-admin-password-" * 3,
        "app_password": "SYNTHETIC-app-password-" * 3,
    }))
    return MODULE.LocalPostgres(root, tmp_path / "synthetic-binaries")


@pytest.mark.parametrize("item", ["OWNER", "credentials.json", "data/PG_VERSION", "data"])
def test_rejects_symlinked_owned_files(database, tmp_path, item):
    destination = database.root / item
    outside = tmp_path / "outside"
    destination.rename(outside)
    destination.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(MODULE.LocalDatabaseError, match="incomplete or unowned"):
        database.owned()


def test_refuses_changed_owner_or_other_database_major(database):
    (database.root / "OWNER").write_text("some-other-project\n")
    with pytest.raises(MODULE.LocalDatabaseError, match="unowned"):
        database.owned()
    (database.root / "OWNER").write_text(MODULE.OWNER + "\n")
    (database.data / "PG_VERSION").write_text("16\n")
    with pytest.raises(MODULE.LocalDatabaseError, match="not PostgreSQL 17"):
        database.owned()


def test_refuses_reinitialization_without_changing_credentials(database, monkeypatch):
    before = (database.root / "credentials.json").read_bytes()
    monkeypatch.setattr(database, "version", lambda: "postgres (PostgreSQL) 17.11")
    with pytest.raises(MODULE.LocalDatabaseError, match="already exists"):
        database.initialize()
    assert (database.root / "credentials.json").read_bytes() == before


def test_rejects_world_readable_credentials(database):
    (database.root / "credentials.json").chmod(0o644)
    with pytest.raises(MODULE.LocalDatabaseError, match="0600"):
        database.credentials()


def test_live_control_lock_excludes_second_operation(database):
    another = MODULE.LocalPostgres(database.root, database.binaries)
    with database.lock():
        with pytest.raises(MODULE.LocalDatabaseError, match="Another local database operation"):
            with another.lock():
                pytest.fail("A second operation acquired the lock")
    with another.lock():
        pass


def test_child_environment_does_not_inherit_database_routes(database, monkeypatch):
    for name in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGOPTIONS", "PGDATABASE"):
        monkeypatch.setenv(name, "SYNTHETIC-UNRELATED-SERVER")
    monkeypatch.setenv("TEST_DATABASE_URL", "SYNTHETIC-test-url")
    monkeypatch.setenv("XHS_ADB_SERIAL", "SYNTHETIC-device")
    env = database.app_environment()
    assert not any(key.startswith("PG") for key in env)
    assert "TEST_DATABASE_URL" not in env
    assert env["XHS_DATABASE_URL"].endswith("@127.0.0.1:55432/xhs_mobile")
    assert env["XHS_ADB_SERIAL"] == "SYNTHETIC-device"
    assert os.environ["PGHOST"] == "SYNTHETIC-UNRELATED-SERVER"


def test_stop_checks_server_identity_before_sending_signal(database, monkeypatch):
    monkeypatch.setattr(database, "running", lambda: True)
    executed = []
    monkeypatch.setattr(database, "command", lambda *a, **kw: executed.append(a))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, *args):
            return self

        def fetchone(self):
            return ("/SYNTHETIC/another/database", "127.0.0.1", "55432", "170011")

    monkeypatch.setattr(database, "connection", lambda **kw: Connection())
    with pytest.raises(MODULE.LocalDatabaseError, match="not the owned"):
        database.stop()
    assert executed == []


def test_failed_backup_never_publishes_a_successful_dump(database, monkeypatch):
    monkeypatch.setattr(database, "status", lambda: {"running": True})

    def fail(name, arguments, **kwargs):
        assert name == "pg_dump"
        Path(arguments[-1]).write_bytes(b"SYNTHETIC-INCOMPLETE-DUMP")
        raise MODULE.LocalDatabaseError("pg_dump failed")

    monkeypatch.setattr(database, "command", fail)
    with pytest.raises(MODULE.LocalDatabaseError, match="pg_dump failed"):
        database.backup()
    assert list((database.root / "backups").iterdir()) == []


def test_verified_backup_is_private_and_explicitly_database_only(database, monkeypatch):
    monkeypatch.setattr(database, "status", lambda: {"running": True})

    def synthetic_dump(name, arguments, **kwargs):
        if name == "pg_dump":
            Path(arguments[-1]).write_bytes(b"SYNTHETIC-NOT-A-REAL-PG-DUMP")
        else:
            assert name == "pg_restore" and arguments[0] == "--list"
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(database, "command", synthetic_dump)
    result = database.backup()
    dump = Path(result["dump"])
    assert result["database_only"] is True
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert stat.S_IMODE(dump.with_suffix(".json").stat().st_mode) == 0o600
    assert "SYNTHETIC-app-password" not in json.dumps(result)
    assert not list(dump.parent.glob("*.partial"))


def test_private_write_never_overwrites_existing_secret(tmp_path):
    path = tmp_path / "credentials"
    MODULE.write_private(path, "SYNTHETIC-original")
    with pytest.raises(FileExistsError):
        MODULE.write_private(path, "SYNTHETIC-overwrite")
    assert path.read_text() == "SYNTHETIC-original"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("version", [
    "postgres (PostgreSQL) 17.11", "postgres (PostgreSQL) 17.11 (Homebrew)",
    "postgres (PostgreSQL) 16.2", "postgres (PostgreSQL) 18.6",
])
def test_only_expected_major_binaries_can_manage_live_cluster(database, monkeypatch, version):
    monkeypatch.setattr(database, "command", lambda *a, **kw: subprocess.CompletedProcess(
        [], 0, version + "\n", ""
    ))
    if "17.11" in version:
        assert database.version() == version
    else:
        with pytest.raises(MODULE.LocalDatabaseError, match="requires PostgreSQL 17"):
            database.version()


def test_changed_network_binding_is_rejected_before_start(database, monkeypatch):
    monkeypatch.setattr(database, "version", lambda: "postgres (PostgreSQL) 17.11")
    monkeypatch.setattr(database, "command", lambda *a, **kw: subprocess.CompletedProcess(
        [], 0, "*\n", ""
    ))
    with pytest.raises(MODULE.LocalDatabaseError, match="listen_addresses"):
        database.configuration_safe()


def test_driver_connection_pins_route_even_with_ambient_pg_hostaddr(database, monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=synthetic_unrelated")
    recorded = {}
    monkeypatch.setattr(MODULE.psycopg, "connect", lambda **kw: recorded.update(kw))
    database.connection()
    assert recorded["host"] == recorded["hostaddr"] == "127.0.0.1"
    assert recorded["dbname"] == "xhs_mobile"
    assert recorded["options"] == ""
    assert recorded["port"] == 55432


def test_status_does_not_emit_credentials(database, monkeypatch):
    monkeypatch.setattr(database, "running", lambda: False)
    monkeypatch.setattr(database, "version", lambda: "postgres (PostgreSQL) 17.11")
    report = json.dumps(database.status())
    assert "SYNTHETIC-app-password" not in report
    assert "SYNTHETIC-admin-password" not in report
    assert "postgresql+psycopg://" not in report
