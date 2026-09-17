"""Local JSONL operational journal; database events remain the authoritative history."""

import json
import os
from pathlib import Path

from xhs_mobile.domain import utcnow


def journal(state_dir: Path, kind: str, detail: dict) -> None:
    directory = state_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {"at": utcnow().isoformat(), "kind": kind, **detail}
    data = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode()
    fd = os.open(directory / "operations.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
