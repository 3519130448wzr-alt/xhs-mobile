"""Faithful database exports, published atomically without replacing existing files."""

import csv
import json
import os
import uuid
from pathlib import Path
from typing import Any

METADATA_COLUMNS = [
    "schema_version", "id", "task_id", "run_id", "device_id", "session_ref", "platform",
    "note_id", "canonical_url", "identity_source", "fingerprint", "eligible", "app_version",
    "captured_at", "review_verdict", "reviewer", "review_identity", "reviewed_at",
]
FIELD_ATTRIBUTES = [
    "raw", "status", "method", "normalized", "approximate", "reason", "confidence", "region",
    "evidence_ref",
]
CSV_ESCAPE_NOTE = (
    "CSV text beginning with formula characters (=,+,-,@), leading control characters, "
    "or whitespace followed by formula characters receives a leading apostrophe. "
    "record_json preserves the original unmodified database record. JSONL is unmodified."
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return _json(value)
    trimmed = value.lstrip()
    if value.startswith(("\t", "\r", "\n")) or trimmed.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _write_csv(records: list[dict], stream) -> None:
    names = sorted({
        name for record in records for name in record.get("data", {}).get("fields", {})
    })
    field_columns = [
        f"field.{name}.{attribute}" for name in names for attribute in FIELD_ATTRIBUTES
    ]
    extra_columns = [
        "body_complete", "content_type", "warnings_json", "evidence_manifest_paths_json",
        "completeness_status", "quality_revision", "time_kind", "topics_status", "topics_json",
        "topic_sources_json", "identity_status", "base_readable",
        "data_json", "evidence_json", "record_json", "csv_text_escape",
    ]
    writer = csv.DictWriter(stream, fieldnames=METADATA_COLUMNS + field_columns + extra_columns)
    writer.writeheader()
    for record in records:
        row = {name: record.get(name) for name in METADATA_COLUMNS}
        data = record.get("data", {})
        for name in names:
            field = data.get("fields", {}).get(name, {})
            for attribute in FIELD_ATTRIBUTES:
                row[f"field.{name}.{attribute}"] = field.get(attribute)
        row.update({
            "body_complete": data.get("body_complete"),
            "content_type": data.get("content_type"),
            "completeness_status": data.get("completeness_status", "legacy_assessment"),
            "quality_revision": data.get("quality_revision", 1),
            "time_kind": data.get("time_kind", "not_readable"),
            "topics_status": data.get("topics_status", "unrecognized"),
            "topics_json": _json(data.get("topics", [])),
            "topic_sources_json": _json(data.get("topic_sources", [])),
            "identity_status": "verified" if record.get("note_id") else "unverified",
            "base_readable": record.get("eligible"),
            "warnings_json": _json(data.get("warnings", [])),
            "evidence_manifest_paths_json": _json([
                item.get("manifest_path") for item in record.get("evidence", [])
            ]),
            "data_json": _json(data),
            "evidence_json": _json(record.get("evidence", [])),
            "record_json": _json(record),
            "csv_text_escape": CSV_ESCAPE_NOTE,
        })
        writer.writerow({key: _csv_value(value) for key, value in row.items()})


def export_records(records: list[dict], output: Path, format: str) -> dict:
    """The caller supplies records read from its repository, never generated notes.

    Hard-link publication is atomic and fails if the destination already exists,
    including a destination created after the initial check. Temporary files are
    on the same filesystem and always have owner-only permissions.
    """
    if format not in {"jsonl", "csv"}:
        raise ValueError("Export format must be jsonl or csv")
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"Export will not overwrite existing path: {output}")
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    stage = output.parent / f".{output.name}.{uuid.uuid4().hex}.pending"
    descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            if format == "jsonl":
                for record in records:
                    stream.write(_json(record) + "\n")
            else:
                _write_csv(records, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike os.replace or rename, this cannot overwrite a racing destination.
        os.link(stage, output)
        stage.unlink()
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        stage.unlink(missing_ok=True)
    result = {"count": len(records), "path": str(output), "format": format}
    if format == "csv":
        result["csv_text_escape"] = CSV_ESCAPE_NOTE
    return result
