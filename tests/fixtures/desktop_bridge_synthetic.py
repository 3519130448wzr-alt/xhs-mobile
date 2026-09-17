"""SYNTHETIC GUI protocol fixture: no collector, database, ADB or network access.

Used only by a separately labelled temporary App bundle for desktop lifecycle QA.
All changing counters are synthetic UI observations and never collection results.
"""

import argparse
import copy
import json
import os
import threading
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--project", type=Path, required=True)
parser.add_argument("--device", default="SYNTHETIC")
args = parser.parse_args()
lock = threading.RLock()
stop = threading.Event()
log = args.project / "var/diagnostics/desktop-synthetic-state.json"
state = {
    "initialized": True, "busy": False, "shutting_down": False, "activity": None,
    "tasks": [], "log_dir": str(log.parent),
    "device": {
        "id": "SYNTHETIC", "status": "offline", "message": "SYNTHETIC 模拟连接失败",
        "console_url": "https://wya.wuying.aliyun.com/instanceLayouts",
        "checks": [
            {"id": "database", "label": "本机数据", "status": "ready",
             "message": "SYNTHETIC 测试状态，不连接真实数据库"},
            {"id": "connection", "label": "云手机连接", "status": "offline",
             "message": "SYNTHETIC 模拟断连，重新检查会恢复"},
        ],
    },
    "issue": {
        "id": "synthetic-offline-incident", "code": "offline",
        "title": "SYNTHETIC 手机连接失败", "message": "这是界面故障演练，不是真实设备状态。",
        "cloud_action": True, "can_retry": True,
    },
}


def emit(value):
    with lock:
        print(json.dumps(value, ensure_ascii=False), flush=True)


def snapshot():
    with lock:
        value = copy.deepcopy(state)
        log.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        log.chmod(0o600)
        journal = log.with_suffix(".jsonl")
        with journal.open("a") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "time": time.time(), "state": value})
                         + "\n")
        journal.chmod(0o600)
        return value


def changed():
    emit({"event": "state", "data": snapshot()})


def run():
    while not stop.wait(1):
        with lock:
            item = state["tasks"][0]
            item["observations"] += 1
            item["eligible"] += 1
            item["updated_at"] = "2026-09-14T12:00:00Z"
            done = item["eligible"] >= item["target"]
        changed()
        if done:
            break
    with lock:
        item = state["tasks"][0]
        item["active"] = False
        item["status"] = "paused" if stop.is_set() else "collected_awaiting_review"
        item["can_resume"] = stop.is_set()
        item["stop_reason"] = "pause_requested" if stop.is_set() else "target_collected"
        state["busy"] = False
        state["activity"] = None
    changed()


def start(params):
    with lock:
        if state["busy"]:
            raise ValueError("SYNTHETIC: 已有任务运行")
        stop.clear()
        state["issue"] = None
        state["tasks"] = [{
            "id": "synthetic-batch", "kind": "batch", "title": "SYNTHETIC 后台运行演练",
            "status": "running", "stop_reason": None, "observations": 0, "eligible": 0,
            "confirmed_identities": 0, "target": params.get("limit", 99),
            "created_at": "2026-09-14T12:00:00Z", "updated_at": "2026-09-14T12:00:00Z",
            "active": True, "can_resume": False, "requires_ack": False,
            "tasks": [], "export_path": None, "cooldown_until": None,
        }]
        state["busy"] = True
        state["activity"] = {
            "phase": "running", "kind": "batch", "id": "synthetic-batch",
            "keyword": "SYNTHETIC", "message": "仅验证桌面交互，计数不是真实采集成果。",
        }
    threading.Thread(target=run, daemon=True).start()
    changed()
    return {"accepted": True}


def dispatch(method, params):
    if method in {"initialize", "status"}:
        return snapshot()
    if method == "check":
        with lock:
            state["device"]["status"] = "ready"
            state["device"]["message"] = "SYNTHETIC 检查通过"
            state["device"]["checks"][1].update(status="ready", message="SYNTHETIC 检查通过")
            state["issue"] = None
        changed()
        return {"accepted": True}
    if method in {"start", "resume"}:
        return start(params)
    if method in {"pause", "shutdown"}:
        with lock:
            state["shutting_down"] = method == "shutdown"
            if state["activity"]:
                state["activity"]["phase"] = "pausing"
        stop.set()
        changed()
        return {"accepted": True}
    if method == "cancel_shutdown":
        with lock:
            state["shutting_down"] = False
        changed()
        return {"accepted": True}
    if method == "detail":
        return {"item": state["tasks"][0], "records": [{
            "id": "synthetic-record", "title": "SYNTHETIC 界面记录",
            "author": "SYNTHETIC 作者", "body": "synthetic 数据，不是真实采集成果。",
            "eligible": True, "quality": "SYNTHETIC", "evidence_paths": [],
        }], "total": 1, "next_offset": None}
    raise ValueError("SYNTHETIC: 不支持此演练操作")


try:
    import sys

    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            result = dispatch(request["method"], request.get("params", {}))
            emit({"id": request["id"], "ok": True, "result": result})
        except (ValueError, KeyError) as error:
            emit({"id": request.get("id"), "ok": False,
                  "error": {"code": "synthetic", "message": str(error)}})
finally:
    stop.set()
    time.sleep(0.2)
