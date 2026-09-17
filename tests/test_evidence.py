"""Synthetic evidence integrity tests, not real-device acceptance evidence."""

import copy
import json
import os

import pytest

from xhs_mobile.domain import EvidenceError, Snapshot
from xhs_mobile.evidence import EvidenceStore


@pytest.fixture
def synthetic_snapshot():
    return Snapshot(
        xml='<hierarchy synthetic="true"><node text="合成证据"/></hierarchy>',
        png=b"synthetic-test-bytes-not-a-real-device-screenshot",
        metadata={"source": "synthetic", "source_kind": "synthetic", "app_version": "synthetic-1"},
    )


def test_private_originals_and_manifest(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path / "evidence")
    manifest = store.save(
        synthetic_snapshot, device_id="synthetic-device", run_id="synthetic-run", label="detail"
    )
    assert store.verify(manifest)
    originals = (("xml", synthetic_snapshot.xml.encode()), ("png", synthetic_snapshot.png))
    for kind, original in originals:
        path = store.root / manifest["files"][kind]["path"]
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    directory = store.root / manifest["evidence_id"]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (store.root / manifest["manifest_path"]).stat().st_mode & 0o777 == 0o600
    assert manifest["metadata"]["source"] == "synthetic"


def test_hash_detects_original_tampering(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path)
    manifest = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    (tmp_path / manifest["files"]["xml"]["path"]).write_bytes(b"changed")
    with pytest.raises(EvidenceError, match="integrity"):
        store.verify(manifest)


def test_manifest_must_match_disk(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path)
    manifest = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    manifest["label"] = "altered"
    with pytest.raises(EvidenceError, match="stored original"):
        store.verify(manifest)


def test_failed_component_never_publishes_manifest(tmp_path, monkeypatch, synthetic_snapshot):
    store = EvidenceStore(tmp_path / "evidence")
    write = store._write

    def fail_png(path, data):
        if path.name == "screen.png":
            raise OSError("synthetic disk full")
        write(path, data)

    monkeypatch.setattr(store, "_write", fail_png)
    with pytest.raises(EvidenceError, match="disk full"):
        store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    assert list(store.root.iterdir()) == []


def test_failed_rename_never_returns_manifest(tmp_path, monkeypatch, synthetic_snapshot):
    store = EvidenceStore(tmp_path / "evidence")

    def fail_rename(*args):
        raise OSError("synthetic failed rename")

    monkeypatch.setattr(type(tmp_path), "rename", fail_rename)
    with pytest.raises(EvidenceError, match="failed rename"):
        store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    assert list(store.root.iterdir()) == []


def test_repeated_captures_preserve_independent_observations(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path)
    first = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    second = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    assert first["evidence_id"] != second["evidence_id"]
    assert store.verify(first) and store.verify(second)


def test_identifiers_are_data_not_paths(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path)
    manifest = store.save(synthetic_snapshot, device_id="../../d", run_id="/run", label="../label")
    assert manifest["device_id"] == "../../d"
    assert len(list(tmp_path.iterdir())) == 1
    assert store.verify(manifest)


def test_verify_rejects_symlink_component(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path / "evidence")
    manifest = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    path = store.root / manifest["files"]["xml"]["path"]
    outside = tmp_path / "outside.xml"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(EvidenceError):
        store.verify(manifest)


def test_verify_rejects_traversal_even_when_disk_manifest_agrees(tmp_path, synthetic_snapshot):
    store = EvidenceStore(tmp_path / "evidence")
    manifest = store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    malicious = copy.deepcopy(manifest)
    malicious["files"]["xml"]["path"] = "../../outside.xml"
    (store.root / manifest["manifest_path"]).write_text(json.dumps(malicious))
    with pytest.raises(EvidenceError, match="component path"):
        store.verify(malicious)


def test_non_serializable_metadata_is_not_committed(tmp_path, synthetic_snapshot):
    synthetic_snapshot.metadata["bad"] = object()
    store = EvidenceStore(tmp_path)
    with pytest.raises(EvidenceError):
        store.save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
    assert list(tmp_path.iterdir()) == []


def test_root_symlink_is_rejected(tmp_path, synthetic_snapshot):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    os.symlink(actual, link)
    with pytest.raises(EvidenceError, match="symbolic link"):
        EvidenceStore(link).save(synthetic_snapshot, device_id="d", run_id="r", label="detail")
