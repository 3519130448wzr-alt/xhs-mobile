"""Own one durable, loopback-only PostgreSQL 17 cluster; never manage test data.

Run with the project virtualenv. Passwords remain in a mode-0600 private file.
There is deliberately no reset, delete, or restore-over-live command.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

PROJECT = Path(__file__).resolve().parents[1]
OWNER = "5507-xhs-durable-postgres-v1"
PORT = 55432
DATABASE = "xhs_mobile"
ADMIN = "xhs_local_admin"
APP_USER = "xhs_app"
HBA = "host all all 127.0.0.1/32 scram-sha-256\n"


class LocalDatabaseError(RuntimeError):
    pass


def clean_environment():
    # Do not inherit service files, PGOPTIONS, or an unrelated database route.
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, value: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


class LocalPostgres:
    def __init__(self, root: Path, binaries: Path):
        self.root = root.absolute()
        self.data = self.root / "data"
        self.binaries = binaries

    def safe_path(self):
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise LocalDatabaseError("Refusing a symlink in the live database path")
        if self.root.exists():
            if not self.root.is_dir() or self.root.stat().st_mode & 0o077:
                raise LocalDatabaseError("Live database directory must be private (mode 0700)")

    @contextlib.contextmanager
    def lock(self):
        self.safe_path()
        self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root.parent / ".postgres-live.control.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LocalDatabaseError("Another local database operation is running") from exc
            yield
        finally:
            os.close(fd)

    def command(self, name, args, *, env=None, check=True, timeout=60):
        result = subprocess.run(
            [str(self.binaries / name), *map(str, args)],
            env=env or clean_environment(),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode:
            # Server/tool errors can contain connection parameters; keep them private.
            raise LocalDatabaseError(
                f"{name} failed (exit {result.returncode}); inspect private log"
            )
        return result

    def version(self):
        version = self.command("postgres", ["--version"]).stdout.strip()
        if not re.fullmatch(r"postgres \(PostgreSQL\) 17\.\d+(?: \(Homebrew\))?", version):
            raise LocalDatabaseError("This live database manager requires PostgreSQL 17 binaries")
        return version

    def owned(self):
        self.safe_path()
        for name in ("OWNER", "credentials.json", "data", "data/PG_VERSION"):
            path = self.root / name
            if path.is_symlink() or not path.exists():
                raise LocalDatabaseError("Live cluster is incomplete or unowned; refusing changes")
        if (self.root / "OWNER").read_text().strip() != OWNER:
            raise LocalDatabaseError("Refusing to manage an unowned database")
        if (self.data / "PG_VERSION").read_text().strip() != "17":
            raise LocalDatabaseError("Live data is not PostgreSQL 17; do not reuse another cluster")
        mode = stat.S_IMODE((self.root / "credentials.json").stat().st_mode)
        if mode != 0o600:
            raise LocalDatabaseError("Credentials must have mode 0600")
        if self.data.stat().st_mode & 0o077:
            raise LocalDatabaseError("PostgreSQL data directory must have mode 0700")

    def credentials(self):
        self.owned()
        result = json.loads((self.root / "credentials.json").read_text())
        if set(result) != {"admin_password", "app_password"} or any(
            not isinstance(value, str) or len(value) < 32 for value in result.values()
        ):
            raise LocalDatabaseError("Invalid local database credential file")
        return result

    def initialize(self):
        self.safe_path()
        self.version()
        if self.root.exists():
            raise LocalDatabaseError(
                "Live database directory already exists; refusing initialization"
            )
        self.root.mkdir(mode=0o700)
        write_private(self.root / "OWNER", OWNER + "\n")
        credentials = {
            "admin_password": secrets.token_urlsafe(48),
            "app_password": secrets.token_urlsafe(48),
        }
        write_private(self.root / "credentials.json", json.dumps(credentials) + "\n")
        password_file = self.root / ".init-password"
        write_private(password_file, credentials["admin_password"] + "\n")
        try:
            self.command("initdb", [
                "-D", self.data, "-U", ADMIN, "--encoding=UTF8", "--locale=C",
                "--auth-host=scram-sha-256", "--auth-local=scram-sha-256",
                "--data-checksums", f"--pwfile={password_file}",
            ])
        finally:
            password_file.unlink(missing_ok=True)
        with (self.data / "postgresql.conf").open("a") as stream:
            stream.write(
                "\n# Project-owned durable live database; not the disposable test cluster.\n"
                "listen_addresses = '127.0.0.1'\n"
                f"port = {PORT}\n"
                "unix_socket_directories = ''\n"
                "password_encryption = 'scram-sha-256'\n"
                "max_connections = 30\nshared_buffers = '64MB'\n"
                "fsync = on\nfull_page_writes = on\nsynchronous_commit = on\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        (self.data / "pg_hba.conf").write_text(HBA)

    def configuration_safe(self):
        self.owned()
        self.version()
        expected = {
            "listen_addresses": "127.0.0.1", "port": str(PORT),
            "unix_socket_directories": "", "password_encryption": "scram-sha-256",
            "fsync": "on", "full_page_writes": "on", "synchronous_commit": "on",
        }
        for name, value in expected.items():
            actual = self.command("postgres", ["-D", self.data, "-C", name]).stdout.strip()
            if actual != value:
                raise LocalDatabaseError(f"Unsafe or unexpected live database setting: {name}")
        hba = self.command("postgres", ["-D", self.data, "-C", "hba_file"]).stdout.strip()
        if Path(hba) != self.data / "pg_hba.conf" or Path(hba).is_symlink():
            raise LocalDatabaseError("Unexpected pg_hba.conf path")
        if Path(hba).read_text() != HBA:
            raise LocalDatabaseError("Live pg_hba.conf must require SCRAM on loopback only")

    def running(self):
        result = self.command("pg_ctl", ["-D", self.data, "status"], check=False)
        if result.returncode not in (0, 3):
            raise LocalDatabaseError("Cannot determine owned PostgreSQL process state")
        return result.returncode == 0

    def connection(self, *, admin=False, database=DATABASE):
        credentials = self.credentials()
        return psycopg.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=PORT, dbname=database,
            user=ADMIN if admin else APP_USER,
            password=credentials["admin_password" if admin else "app_password"],
            connect_timeout=5, options="", sslmode="disable", autocommit=True,
        )

    def server_identity(self, connection):
        row = connection.execute(
            "SELECT current_setting('data_directory'), current_setting('listen_addresses'), "
            "current_setting('port'), current_setting('server_version_num')"
        ).fetchone()
        if row[:3] != (str(self.data), "127.0.0.1", str(PORT)) or not (
            170000 <= int(row[3]) < 180000
        ):
            raise LocalDatabaseError("Connected server is not the owned live database")

    def provision(self):
        with self.connection(admin=True, database="postgres") as connection:
            self.server_identity(connection)
            row = connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=%s",
                (APP_USER,),
            ).fetchone()
            if row is None:
                connection.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(APP_USER), sql.Literal(self.credentials()["app_password"])))
            elif row != (False, False, False):
                raise LocalDatabaseError("Existing application role has unexpected privileges")
            row = connection.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (DATABASE,)
            ).fetchone()
            if row is None:
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(DATABASE), sql.Identifier(APP_USER)
                ))
            elif row[0] != APP_USER:
                raise LocalDatabaseError("Unexpected live database owner")

    def start(self):
        if not self.root.exists():
            self.initialize()
        self.configuration_safe()
        if not self.running():
            with socket.socket() as check:
                try:
                    check.bind(("127.0.0.1", PORT))
                except OSError as exc:
                    raise LocalDatabaseError(
                        "Live database port is occupied; no server started"
                    ) from exc
            self.command("pg_ctl", [
                "-D", self.data, "-l", self.root / "postgres.log", "-w", "-t", "30", "start",
            ], timeout=40)
        self.provision()
        return self.status()

    def stop(self):
        self.owned()
        if self.running():
            # Check the destination before pg_ctl sends a signal.
            with self.connection(admin=True, database="postgres") as connection:
                self.server_identity(connection)
            self.command("pg_ctl", ["-D", self.data, "-m", "fast", "-w", "-t", "30", "stop"])
        return {"running": False, "data_directory": str(self.data), "data_preserved": True}

    def status(self):
        self.safe_path()
        if not self.root.exists():
            return {"initialized": False, "running": False}
        self.owned()
        result = {
            "initialized": True, "running": self.running(), "version": self.version(),
            "host": "127.0.0.1", "port": PORT, "database": DATABASE,
            "data_directory": str(self.data), "credentials_printed": False,
        }
        if result["running"]:
            self.configuration_safe()
            with self.connection(admin=True, database="postgres") as connection:
                self.server_identity(connection)
            with self.connection() as connection:
                revision = connection.execute(
                    "SELECT to_regclass('public.alembic_version')"
                ).fetchone()
                result["migration"] = None if revision[0] is None else connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
        return result

    def app_environment(self):
        env = clean_environment()
        password = quote(self.credentials()["app_password"], safe="")
        env["XHS_DATABASE_URL"] = (
            f"postgresql+psycopg://{APP_USER}:{password}@127.0.0.1:{PORT}/{DATABASE}"
        )
        env.pop("TEST_DATABASE_URL", None)
        return env

    def xhs(self, arguments):
        with self.lock():
            if not self.status().get("running"):
                raise LocalDatabaseError("Start the local live database first")
            env = self.app_environment()
        # Release the database control lock before a long-running crawler starts.
        return subprocess.run(
            [sys.executable, "-m", "xhs_mobile.cli", *arguments], cwd=PROJECT, env=env,
        ).returncode

    def backup(self):
        if not self.status().get("running"):
            raise LocalDatabaseError("Start the local live database before backing it up")
        backup_root = self.root / "backups"
        if backup_root.is_symlink():
            raise LocalDatabaseError("Refusing a symlink backup directory")
        backup_root.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = backup_root / f"{stamp}.dump"
        temporary = destination.with_suffix(".partial")
        env = clean_environment()
        env.update({
            "PGHOST": "127.0.0.1", "PGPORT": str(PORT), "PGDATABASE": DATABASE,
            "PGUSER": APP_USER, "PGPASSWORD": self.credentials()["app_password"],
            "PGCONNECT_TIMEOUT": "5",
        })
        try:
            self.command("pg_dump", ["-w", "-Fc", "-f", temporary], env=env, timeout=300)
            temporary.chmod(0o600)
            self.command("pg_restore", ["--list", temporary], timeout=30)
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.fsync(stream.fileno())
            temporary.rename(destination)
            sync_directory(backup_root)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        result = {
            "database": DATABASE, "dump": str(destination), "sha256": digest,
            "database_only": True, "evidence_directory": str(PROJECT / "var" / "evidence"),
            "note": "Also preserve var/evidence; see docs/LOCAL_DATABASE.md before a full backup.",
        }
        write_private(destination.with_suffix(".json"), json.dumps(result, indent=2) + "\n")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status", "stop", "backup", "migrate", "xhs"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    database = LocalPostgres(PROJECT / "var" / "postgres-live", Path(
        "/opt/homebrew/opt/postgresql@17/bin"
    ))
    try:
        if args.command in ("xhs", "migrate"):
            arguments = args.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            if args.command == "migrate":
                if arguments:
                    parser.error("migrate does not accept extra arguments")
                arguments = ["db", "upgrade"]
            return database.xhs(arguments)
        if args.arguments:
            parser.error("This operation does not accept extra arguments")
        with database.lock():
            result = getattr(database, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        LocalDatabaseError, OSError, ValueError, psycopg.Error, subprocess.SubprocessError
    ) as exc:
        # Never emit database passwords, DSNs, or raw driver/subprocess exceptions.
        detail = str(exc) if isinstance(exc, LocalDatabaseError) else type(exc).__name__
        print(json.dumps({"error": "local_database_error", "detail": detail}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
