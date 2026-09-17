"""SYNTHETIC bundle export tests; no rows here are real collection results."""

import csv
import hashlib
import json
import stat
from copy import deepcopy

import pytest

from xhs_mobile import batch_exports as module


@pytest.fixture
def data():
    batch = {
        "id": "SYNTHETIC-batch", "status": "partial",
        "tasks": [
            {"id": "SYNTHETIC-task-1", "keyword": "SYNTHETIC 甲", "target": 10,
             "status": "collected_awaiting_review", "observation_count": 99},
            {"id": "SYNTHETIC-task-2", "keyword": "SYNTHETIC 乙", "target": 10,
             "status": "partial", "observation_count": 99},
        ],
    }
    records = []
    for index in range(2):
        records.append({
            "id": f"SYNTHETIC-row-{index}", "task_id": batch["tasks"][index]["id"],
            "note_id": None, "eligible": index == 0, "review_verdict": None,
            "data": {"body_complete": index == 0, "fields": {
                "title": {"raw": "=SYNTHETIC 标题", "status": "present"},
                "body": {"raw": "SYNTHETIC 中文🥕\n下一行", "status": "present"},
                "likes": {"raw": "1.2万", "normalized": 12000, "approximate": True},
                "comments": {"raw": None, "normalized": None, "status": "not_readable"},
            }},
            "evidence": [{"source_kind": "synthetic", "manifest_path": "SYNTHETIC/path"}],
        })
    return records, batch


def test_bundle_preserves_both_formats_and_publishes_truthful_manifest(tmp_path, data):
    records, batch = data
    original = deepcopy(records)
    output = tmp_path / "SYNTHETIC-bundle"
    result = module.export_bundle(records, batch, output)
    assert result == {"ok": True, "count": 2, "path": str(output), "format": "bundle"}
    assert not (output / "INCOMPLETE.txt").exists()
    with (output / "notes.csv").open(newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    jsonl_rows = [json.loads(line) for line in (output / "notes.jsonl").read_text().splitlines()]
    assert jsonl_rows == [json.loads(row["record_json"]) for row in csv_rows] == original
    assert csv_rows[0]["field.title.raw"] == "'=SYNTHETIC 标题"
    assert csv_rows[0]["field.comments.normalized"] == ""
    assert records == original
    manifest = json.loads((output / "summary.json").read_text())
    assert manifest["complete"] is True
    assert manifest["program_status_at_export"] == "partial"
    assert manifest["observation_count"] == 2
    assert manifest["real_device_acceptance"] == "not_inferred"
    assert [task["observations_in_export"] for task in manifest["tasks"]] == [1, 1]
    assert [task["eligible_observations_in_export"] for task in manifest["tasks"]] == [1, 0]
    for item in manifest["files"]:
        assert hashlib.sha256((output / item["file"]).read_bytes()).hexdigest() == item["sha256"]
        assert item["count"] == 2
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())


@pytest.mark.parametrize("existing_kind", ["directory", "file", "symlink"])
def test_bundle_never_replaces_existing_output(tmp_path, data, existing_kind):
    records, batch = data
    output = tmp_path / "existing"
    if existing_kind == "directory":
        output.mkdir()
        (output / "keep.txt").write_text("SYNTHETIC previous result")
    elif existing_kind == "file":
        output.write_text("SYNTHETIC previous result")
    else:
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("SYNTHETIC previous result")
        output.symlink_to(target)
    with pytest.raises(FileExistsError):
        module.export_bundle(records, batch, output)
    original = output if existing_kind == "file" else output / "keep.txt"
    assert original.read_text() == "SYNTHETIC previous result"


def test_second_format_failure_keeps_incomplete_marker_and_never_publishes_summary(
    tmp_path, data, monkeypatch,
):
    records, batch = data
    original = module.export_records

    def fail_csv(rows, output, format):
        if format == "csv":
            raise OSError("SYNTHETIC disk full")
        return original(rows, output, format)

    monkeypatch.setattr(module, "export_records", fail_csv)
    output = tmp_path / "failed-bundle"
    with pytest.raises(OSError, match="SYNTHETIC"):
        module.export_bundle(records, batch, output)
    assert (output / "INCOMPLETE.txt").exists()
    assert (output / "notes.jsonl").exists()
    assert not (output / "notes.csv").exists()
    assert not (output / "summary.json").exists()
    with pytest.raises(FileExistsError):
        module.export_bundle(records, batch, output)


def test_summary_publication_failure_retains_incomplete_marker(tmp_path, data, monkeypatch):
    records, batch = data
    original = module.os.link

    def fail_summary(source, target):
        if target.name == "summary.json":
            raise OSError("SYNTHETIC summary publication failure")
        return original(source, target)

    monkeypatch.setattr(module.os, "link", fail_summary)
    output = tmp_path / "failed-summary"
    with pytest.raises(OSError, match="SYNTHETIC"):
        module.export_bundle(records, batch, output)
    assert (output / "INCOMPLETE.txt").exists()
    assert (output / "notes.jsonl").exists() and (output / "notes.csv").exists()
    assert not (output / "summary.json").exists()


def test_empty_batch_export_counts_zero_without_claiming_acceptance(tmp_path, data):
    _, batch = data
    output = tmp_path / "empty"
    module.export_bundle([], batch, output)
    manifest = json.loads((output / "summary.json").read_text())
    assert manifest["observation_count"] == 0
    assert all(task["observations_in_export"] == 0 for task in manifest["tasks"])
    assert manifest["real_device_acceptance"] == "not_inferred"
    assert (output / "notes.jsonl").read_text() == ""
    assert "record_json" in (output / "notes.csv").read_text()
