"""Export both formats from one frozen record list; publish a completion manifest last."""

import hashlib
import json
import os
from pathlib import Path

from xhs_mobile.domain import utcnow
from xhs_mobile.exports import export_records


def export_bundle(records: list[dict], batch: dict, output: Path) -> dict:
    output = output.absolute()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    marker = output / "INCOMPLETE.txt"
    marker.write_text("导出尚未完成，请勿将此目录视为完整结果。\n", encoding="utf-8")
    marker.chmod(0o600)
    exported = []
    for format in ("jsonl", "csv"):
        path = output / f"notes.{format}"
        export_records(records, path, format)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        exported.append({"file": path.name, "sha256": digest, "count": len(records)})
    tasks = []
    for task in batch["tasks"]:
        rows = [row for row in records if row["task_id"] == task["id"]]
        tasks.append({
            "task_id": task["id"], "keyword": task["keyword"], "target": task["target"],
            "status_at_export": task["status"], "observations_in_export": len(rows),
            "eligible_observations_in_export": sum(row["eligible"] for row in rows),
        })
    manifest = {
        "schema_version": 1, "complete": True, "batch_id": batch["id"],
        "exported_at": utcnow().isoformat(), "program_status_at_export": batch["status"],
        "observation_count": len(records), "tasks": tasks, "files": exported,
        "real_device_acceptance": "not_inferred",
        "note": "CSV 与 JSONL 来自同一次数据库读取；观察数量不代表不同笔记数。",
    }
    pending = output / ".summary.pending"
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    # INCOMPLETE always takes precedence; success is announced only after both publications.
    os.link(pending, output / "summary.json")
    pending.unlink()
    marker.unlink()
    directory = os.open(output, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"ok": True, "count": len(records), "path": str(output), "format": "bundle"}
