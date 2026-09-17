"""SYNTHETIC JSONL pipe tests; no production runtime or phone is initialized."""

import importlib.util
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace


def bridge_module():
    path = Path(__file__).resolve().parents[1] / "scripts/desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("synthetic_desktop_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticController:
    def __init__(self):
        self.runtime = SimpleNamespace(pause=lambda: None)
        self.requests = []
        self.closed = False
        self.emit = lambda value: None

    def snapshot(self):
        return {"busy": False, "activity": None, "shutting_down": False}

    def request(self, value):
        self.requests.append(value)
        return {"id": value["id"], "ok": True, "result": {"text": "SYNTHETIC 中文"}}

    def close(self):
        self.closed = True


def test_jsonl_multibyte_fragmentation_multiple_buffered_requests_and_eof():
    module = bridge_module()
    controller = SyntheticController()
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=0)
    output = io.StringIO()
    worker = threading.Thread(target=module.serve, args=(controller, source, output))
    worker.start()
    first = json.dumps({"id": "1", "method": "status", "params": {"keyword": "SYNTHETIC 中文"}},
                       ensure_ascii=False).encode() + b"\n"
    second = b'{"id":"2","method":"status"}\n'
    chinese_position = first.index("中".encode())
    os.write(write_fd, first[:chinese_position + 1])
    os.write(write_fd, first[chinese_position + 1:] + second)
    os.close(write_fd)
    worker.join(3)
    source.close()
    assert not worker.is_alive() and controller.closed
    assert [r["id"] for r in controller.requests] == ["1", "2"]
    values = [json.loads(line) for line in output.getvalue().splitlines()]
    assert values[0]["event"] == "state"
    assert [v["id"] for v in values if "id" in v] == ["1", "2"]


def test_invalid_json_does_not_desynchronize_next_request():
    module = bridge_module()
    controller = SyntheticController()
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=0)
    output = io.StringIO()
    os.write(write_fd, b'not-json\n{"id":"valid","method":"status"}\n')
    os.close(write_fd)
    module.serve(controller, source, output)
    source.close()
    values = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(v.get("error", {}).get("code") == "invalid_request" for v in values)
    assert controller.requests == [{"id": "valid", "method": "status"}]
    assert controller.closed


def test_stdout_failure_requests_safe_stop_and_close():
    module = bridge_module()
    controller = SyntheticController()
    paused = []
    controller.runtime.pause = lambda: paused.append(True)
    class ClosedOutput:
        def write(self, value):
            raise BrokenPipeError("SYNTHETIC App exited")
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=0)
    os.close(write_fd)
    module.serve(controller, source, ClosedOutput())
    source.close()
    assert paused and controller.closed
