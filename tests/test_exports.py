import csv
import json
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from xhs_mobile.exports import export_records


@pytest.fixture
def records():
    # SYNTHETIC database rows for export testing; no real mobile observations.
    return [{
        "schema_version": 1, "id": "SYNTHETIC-row", "task_id": "SYNTHETIC-task",
        "device_id": "SYNTHETIC-device", "session_ref": "SYNTHETIC-session",
        "run_id": "SYNTHETIC-run", "platform": "xiaohongshu", "note_id": None,
        "captured_at": "2026-09-12T00:00:00+00:00", "review_verdict": "reject",
        "reviewer": "SYNTHETIC审核", "review_identity": None,
        "eligible": False, "app_version": "SYNTHETIC-1",
        "data": {
            "content_type": "image_text", "body_complete": False,
            "fields": {
                "title": {"raw": "=SYNTHETIC公式", "status": "present", "method": "ui"},
                "body": {"raw": "SYNTHETIC 中文,逗号\n换行", "status": "present", "method": "ui"},
                "author": {"raw": "@SYNTHETIC作者", "status": "present", "method": "ui"},
                "likes": {"raw": "1.2万", "status": "present", "method": "ui",
                          "normalized": 12000, "approximate": True, "confidence": None},
                "comments": {"raw": None, "status": "not_readable", "method": "none",
                             "normalized": None, "reason": "SYNTHETIC_missing"},
            },
            "warnings": ["SYNTHETIC"],
        },
        "evidence": [{"manifest_path": "SYNTHETIC/manifest.json",
                      "metadata": {"source_kind": "synthetic"},
                      "files": {"xml": {"path": "SYNTHETIC/ui.xml"}}}],
        "future_nested_schema": {"must_survive": ["中文", 1, None]},
    }]


def test_jsonl_is_exact_and_private(tmp_path, records):
    path = tmp_path / "new" / "records.jsonl"
    report = export_records(records, path, "jsonl")
    assert report["count"] == 1
    assert [json.loads(line) for line in path.read_text().splitlines()] == records
    assert "中文" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_csv_flat_fields_and_lossless_nested_record(tmp_path, records):
    original = deepcopy(records)
    path = tmp_path / "records.csv"
    report = export_records(records, path, "csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    row = rows[0]
    assert row["field.title.raw"] == "'=SYNTHETIC公式"
    assert row["field.author.raw"] == "'@SYNTHETIC作者"
    assert row["field.body.raw"] == "SYNTHETIC 中文,逗号\n换行"
    assert row["field.likes.normalized"] == "12000"
    assert row["field.likes.approximate"] == "true"
    assert row["field.comments.normalized"] == ""
    assert json.loads(row["record_json"]) == original[0]
    assert json.loads(row["data_json"]) == original[0]["data"]
    assert json.loads(row["evidence_json"]) == original[0]["evidence"]
    assert report["csv_text_escape"]
    assert records == original


@pytest.mark.parametrize("value", ["+SYNTHETIC", "-SYNTHETIC", "@SYNTHETIC", " =1", "\tplain"])
def test_csv_spreadsheet_formula_prefixes(tmp_path, records, value):
    records[0]["reviewer"] = value
    path = tmp_path / "records.csv"
    export_records(records, path, "csv")
    with path.open(newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["reviewer"] == "'" + value
    assert json.loads(row["record_json"])["reviewer"] == value


def test_no_overwrite_even_symlink(tmp_path, records):
    old = tmp_path / "old"
    old.write_text("keep")
    for target in (old, tmp_path / "symlink"):
        if target != old:
            target.symlink_to(old)
        with pytest.raises(FileExistsError):
            export_records(records, target, "jsonl")
    assert old.read_text() == "keep"


def test_atomic_publication_rejects_racing_output(tmp_path, records, monkeypatch):
    path = tmp_path / "records.jsonl"
    original_link = os.link

    def race(source, target):
        Path(target).write_text("other process result")
        original_link(source, target)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        export_records(records, path, "jsonl")
    assert path.read_text() == "other process result"
    assert list(tmp_path.glob("*.pending")) == []


def test_serialization_failure_publishes_nothing(tmp_path, records):
    records[0]["unexpected"] = float("nan")
    path = tmp_path / "records.jsonl"
    with pytest.raises(ValueError):
        export_records(records, path, "jsonl")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_empty_exports_and_invalid_format(tmp_path):
    path = tmp_path / "empty.csv"
    assert export_records([], path, "csv")["count"] == 0
    assert "record_json" in path.read_text()
    with pytest.raises(ValueError):
        export_records([], tmp_path / "bad", "xlsx")
