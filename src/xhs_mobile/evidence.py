"""Private, content-checked local evidence committed by atomic directory rename."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .domain import EvidenceError, Snapshot


class EvidenceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def save(
        self, snapshot: Snapshot, *, device_id: str, run_id: str, label: str
    ) -> dict[str, Any]:
        """Return a manifest only after both originals and manifest are durable.

        An interrupted save can leave an unreferenced .pending-* directory, never
        a returned half-manifest. No cleanup silently deletes old source evidence.
        """
        evidence_id = uuid.uuid4().hex
        stage = self.root / f".pending-{evidence_id}"
        final = self.root / evidence_id
        try:
            if not isinstance(snapshot.xml, str) or not snapshot.xml.strip():
                raise ValueError("Missing UI hierarchy")
            if not isinstance(snapshot.png, bytes) or not snapshot.png:
                raise ValueError("Missing screenshot")
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if self.root.is_symlink():
                raise ValueError("Evidence root must not be a symbolic link")
            self.root.chmod(0o700)
            stage.mkdir(mode=0o700)
            originals = {
                "xml": ("ui.xml", snapshot.xml.encode("utf-8")),
                "png": ("screen.png", snapshot.png),
            }
            files = {}
            for kind, (name, data) in originals.items():
                self._write(stage / name, data)
                files[kind] = {
                    "path": f"{evidence_id}/{name}",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            manifest = {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "manifest_path": f"{evidence_id}/manifest.json",
                "device_id": device_id,
                "run_id": run_id,
                "label": label,
                "captured_at": snapshot.captured_at.isoformat(),
                "metadata": snapshot.metadata,
                "files": files,
            }
            encoded = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ).encode("utf-8")
            self._write(stage / "manifest.json", encoded)
            directory_fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            stage.rename(final)
            root_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            # Normalize JSON-compatible metadata to exactly match the disk form.
            return json.loads(encoded)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise EvidenceError(f"Evidence save failed: {exc}") from exc

    def _safe_path(self, relative: str) -> Path:
        if self.root.is_symlink():
            raise EvidenceError("Evidence root must not be a symbolic link")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise EvidenceError("Evidence path must be relative")
        path = self.root / relative
        root = self.root.resolve()
        if not path.resolve().is_relative_to(root) or ".." in Path(relative).parts:
            raise EvidenceError("Evidence path escapes configured root")
        cursor = path
        while cursor != self.root:
            if cursor.is_symlink():
                raise EvidenceError("Evidence contains a symbolic link")
            cursor = cursor.parent
        return path

    def verify(self, manifest: dict[str, Any]) -> bool:
        """Verify manifest agreement and exact hashes; never modify originals."""
        try:
            if manifest["schema_version"] != 1:
                raise EvidenceError("Unsupported evidence schema")
            evidence_id = manifest["evidence_id"]
            if not isinstance(evidence_id, str) or len(evidence_id) != 32:
                raise EvidenceError("Invalid evidence ID")
            if any(c not in "0123456789abcdef" for c in evidence_id):
                raise EvidenceError("Invalid evidence ID")
            if manifest["manifest_path"] != f"{evidence_id}/manifest.json":
                raise EvidenceError("Manifest path does not match evidence ID")
            stored = json.loads(
                self._safe_path(manifest["manifest_path"]).read_text(encoding="utf-8")
            )
            if stored != manifest:
                raise EvidenceError("Manifest does not match its stored original")
            for kind, name in (("xml", "ui.xml"), ("png", "screen.png")):
                entry = manifest["files"][kind]
                if entry["path"] != f"{evidence_id}/{name}":
                    raise EvidenceError("Unexpected evidence component path")
                data = self._safe_path(entry["path"]).read_bytes()
                if (
                    len(data) != entry["size_bytes"]
                    or hashlib.sha256(data).hexdigest() != entry["sha256"]
                ):
                    raise EvidenceError(f"Evidence {kind} failed integrity verification")
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvidenceError(f"Evidence verification failed: {exc}") from exc
