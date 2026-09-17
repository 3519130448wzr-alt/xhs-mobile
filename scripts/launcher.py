"""Local Mac operator launcher. Opening the menu never starts collection.

Reuse the production CLI, its device lock, and persistent task/policy state.
No shell evaluation, automatic review, or automatic restriction acknowledgement.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from local_postgres import LocalDatabaseError, LocalPostgres

from xhs_mobile.config import load_settings
from xhs_mobile.connection import ConnectionFailure, ConnectionManager
from xhs_mobile.domain import DeviceError
from xhs_mobile.locking import DeviceLock
from xhs_mobile.profile import load_profile

PROJECT = Path(__file__).resolve().parents[1]
STATUS_NAMES = {
    "pending": "待启动", "running": "运行中 / 中断后可恢复", "paused": "已暂停",
    "partial": "部分完成", "cooldown": "冷却中", "needs_attention": "需要人工处理",
    "collected_awaiting_review": "采集结束，待人工核验",
}


class LauncherError(RuntimeError):
    pass


def documents(text: str) -> list[dict]:
    """Read successive pretty JSON documents, tolerating non-JSON log lines."""
    decoder, result, position = json.JSONDecoder(), [], 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            end = text.find("\n", position)
            position = len(text) if end < 0 else end + 1
        else:
            if isinstance(value, dict):
                result.append(value)
            position = end
    return result


def display(value) -> str:
    # App/user strings must not emit terminal control sequences.
    return "".join(c if c.isprintable() else " " for c in str(value))


def task_from(values: list[dict]) -> dict | None:
    for value in reversed(values):
        if value.get("tasks"):
            return value["tasks"][0]
    return None


def batch_from(values: list[dict]) -> dict | None:
    for value in reversed(values):
        if value.get("batches"):
            return value["batches"][0]
    return None


class Launcher:
    def __init__(self, project: Path = PROJECT, *, device_id="lab01"):
        self.project = project.resolve()
        self.device_id = device_id
        self.config_path = self.project / "config.local.toml"
        self.settings = load_settings(self.config_path)
        self.device_config = self.settings.device(device_id)
        self.database = LocalPostgres(
            self.project / "var" / "postgres-live",
            Path("/opt/homebrew/opt/postgresql@17/bin"),
        )
        self.serial = ""
        self.env = None
        self.exit_requested = False
        self.cancel_requested = False
        self.log_dir = self.settings.state_dir / "launcher" / datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        self.log_dir.mkdir(mode=0o700, parents=True)
        self.counter = 0

    def execute(self, arguments: list[str], *, env=None, timeout=None) -> tuple[int, list[dict]]:
        self.counter += 1
        log_path = self.log_dir / f"{self.counter:02d}.log"
        with log_path.open("x", encoding="utf-8") as output:
            log_path.chmod(0o600)
            process = subprocess.Popen(
                arguments, cwd=self.project, env=env, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            previous = {}

            def pause(signum, _frame):
                self.cancel_requested = True
                if signum != signal.SIGINT:
                    self.exit_requested = True
                print("\n正在请求安全暂停，请等待当前动作结束…", flush=True)
                # Signal the CLI only. Its Android subprocess may finish the current action.
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(signal.SIGINT)

            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, pause)
            started, next_update = time.monotonic(), time.monotonic() + 20
            seen_progress = set()
            timed_out = False
            try:
                while process.poll() is None:
                    for event in documents(log_path.read_text(encoding="utf-8", errors="replace")):
                        kind = event.get("event")
                        if kind not in {"batch_task_started", "batch_task_finished"}:
                            continue
                        key = (kind, event.get("task_id"))
                        if key in seen_progress:
                            continue
                        seen_progress.add(key)
                        if kind == "batch_task_started":
                            print(f"正在采集：{display(event.get('keyword', ''))}", flush=True)
                        else:
                            print(
                                f"本关键词已保存 {event.get('observations', 0)} 条；"
                                f"合格观察 {event.get('eligible_observations', 0)} 条。",
                                flush=True,
                            )
                    if timeout is not None and time.monotonic() - started > timeout:
                        timed_out = True
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        break
                    if time.monotonic() >= next_update:
                        print("仍在运行，可在云手机预览中查看；Ctrl+C 可安全暂停。", flush=True)
                        next_update = time.monotonic() + 20
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                process.wait()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        if timed_out:
            raise LauncherError(f"启动检查超时，详情保存在 {log_path}")
        values = documents(log_path.read_text(encoding="utf-8", errors="replace"))
        return process.returncode, values

    def cli(self, *arguments: str, timeout=None):
        if self.env is None:
            raise LauncherError("数据库尚未准备完成")
        return self.execute(
            [sys.executable, "-m", "xhs_mobile.cli", "--config", str(self.config_path),
             *arguments], env=self.env, timeout=timeout,
        )

    def require(self, result, message):
        code, values = result
        if code != 0:
            print(f"检查未通过：{message}。诊断日志：{self.log_dir}")
            # Error messages can include connection settings; keep raw details in private logs.
            raise LauncherError(message)
        return values

    def prepare_database(self):
        print("正在准备数据库…", flush=True)
        with self.database.lock():
            self.database.start()
            if not self.database.status().get("running"):
                raise LauncherError("数据库未能启动")
            self.env = self.database.app_environment()
        self.require(self.cli("db", "upgrade", timeout=90), "数据库迁移失败")
        if self.cancel_requested:
            raise LauncherError("已取消本次启动")

    def connection(self):
        path = self.settings.state_dir / "connection" / f"mobile-{self.device_id}.json"
        try:
            config = json.loads(path.read_text(encoding="utf-8"))["adb"]
            serial = config["local_serial"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LauncherError("缺少云手机接入记录，请检查 var/connection 下的设备配置") from exc
        if not isinstance(serial, str) or not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", serial):
            raise LauncherError("本机启动器要求已配置的回环 ADB 连接")
        if not 1 <= int(serial.rsplit(":", 1)[1]) <= 65535:
            raise LauncherError("ADB 端口配置无效")
        return config, serial

    def prepare_device(self):
        profile = load_profile(self.settings.profile_path)
        _, self.serial = self.connection()
        if profile.app_package != self.device_config.app_package:
            raise LauncherError("页面规则与手机 App 配置不匹配")
        self.env[self.device_config.serial_env] = self.serial
        print(f"正在检查云手机 {self.device_id}…", flush=True)
        try:
            manager = ConnectionManager(
                self.settings, self.device_id, project=self.project,
                progress=lambda value: print(
                    display(value.get("message", "正在连接手机")), flush=True,
                ),
                stop_requested=lambda: self.cancel_requested or self.exit_requested,
            )
            self.serial = manager.ensure_connected()
        except ConnectionFailure as exc:
            raise LauncherError(str(exc)) from exc
        reports = self.require(
            self.cli("doctor", "--device", self.device_id, timeout=180),
            "手机检查失败：确认手机开机、ADB 连接及小红书安装正常",
        )
        if self.cancel_requested:
            raise LauncherError("已取消本次启动")
        report = next((r for r in reports if "health" in r), {})
        if report.get("health", {}).get("checks", {}).get("app_version") != profile.app_version:
            raise LauncherError("小红书版本与页面规则不一致，需要重新校准")
        print("数据库和手机已就绪。", flush=True)

    def tasks(self):
        values = self.require(self.cli("status", timeout=30), "读取任务失败")
        return next((v["tasks"] for v in values if "tasks" in v), [])

    def show_task(self, task):
        print(f"\n任务：{task['id']}")
        print(f"状态：{STATUS_NAMES.get(task['status'], display(task['status']))}")
        print(f"关键词：{display(task['keyword'])}")
        print(f"已保存 {task['observation_count']} 条；合格观察 {task['eligible_count']} 条；"
              f"人工已确认身份 {task['human_verified_unique']} 篇。")
        if task.get("stop_reason"):
            print(f"停止原因：{display(task['stop_reason'])}")

    def pick_task(self):
        tasks = [t for t in reversed(self.tasks()) if t["device_id"] == self.device_id][:20]
        if not tasks:
            print("尚无任务。")
            return None
        for index, task in enumerate(tasks, 1):
            print(f"{index}. {display(task['keyword'])} · "
                  f"{STATUS_NAMES.get(task['status'], display(task['status']))} · "
                  f"{task['observation_count']} 条 · {task['id'][:8]}")
        value = input("选择任务编号（回车返回）：").strip()
        if not value:
            return None
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= len(tasks):
            raise LauncherError("任务编号无效")
        return tasks[int(value) - 1]

    def export_task(self, task):
        folder = self.settings.state_dir / "exports" / task["id"] / datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        for format in ("jsonl", "csv"):
            self.require(self.cli(
                "export", "--task", task["id"], "--format", format,
                "--output", str(folder / f"notes.{format}"), timeout=60,
            ), f"{format.upper()} 导出失败，数据库已提交记录仍保留")
        print(f"\n数据已导出到：{folder}")
        print("CSV 可用表格软件打开；JSONL 保留完整字段和来源证据。")
        if sys.platform == "darwin" and sys.stdin.isatty() and not self.exit_requested:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["/usr/bin/open", str(folder)], check=False, timeout=10)
        return folder

    def collect(self, arguments: list[str], *, task_id=None):
        self.prepare_device()
        if self.exit_requested or self.cancel_requested:
            return
        print("\n开始采集。按 Ctrl+C 安全暂停，等结果出现后再关闭窗口。", flush=True)
        code, values = self.cli(*arguments)
        task = task_from(values)
        if task is None:
            started = next((v for v in values if v.get("event") == "run_started"), {})
            task_id = task_id or started.get("task_id")
            if task_id:
                # Includes storage errors after a committed checkpoint; query, never fabricate.
                _, values = self.cli("status", "--task", task_id, timeout=30)
                task = task_from(values)
        if task is not None:
            self.show_task(task)
            self.export_task(task)
            print("采集条数不等于验收通过；请继续人工核对内容和不同笔记身份。")
        if code not in (0, 3) or task is None:
            raise LauncherError(f"本次运行未正常结束。请查看任务状态与日志：{self.log_dir}")

    def show_batch(self, batch):
        print(f"\n批次：{batch['id']}")
        print(f"状态：{STATUS_NAMES.get(batch['status'], display(batch['status']))}")
        for index, task in enumerate(batch["tasks"], 1):
            print(f"{index}. {display(task['keyword'])} · "
                  f"{STATUS_NAMES.get(task['status'], display(task['status']))} · "
                  f"已保存 {task['observation_count']} 条 / 合格观察 {task['eligible_count']} 条"
                  f" / 目标 {task['target']} 条")
        if batch.get("stop_reason"):
            print(f"停止原因：{display(batch['stop_reason'])}")

    def pick_batch(self):
        values = self.require(self.cli("batch", "status", timeout=30), "读取批次失败")
        rows = next((v["batches"] for v in values if "batches" in v), [])
        rows = [row for row in reversed(rows) if row["device_id"] == self.device_id][:20]
        if not rows:
            print("尚无批次。请先选择菜单 6。")
            return None
        for index, row in enumerate(rows, 1):
            keywords = "、".join(display(t["keyword"]) for t in row["tasks"])
            print(f"{index}. {keywords} · "
                  f"{STATUS_NAMES.get(row['status'], display(row['status']))} · {row['id'][:8]}")
        value = input("选择批次编号（回车返回）：").strip()
        if not value:
            return None
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= len(rows):
            raise LauncherError("批次编号无效")
        return rows[int(value) - 1]

    def export_batch(self, batch):
        folder = self.settings.state_dir / "exports" / "batches" / batch["id"] / (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        self.require(self.cli(
            "batch", "export", "--batch", batch["id"], "--format", "bundle",
            "--output", str(folder), timeout=90,
        ), "批次导出失败；可用菜单 8 从数据库重新导出")
        print(f"\n批次结果已导出：{folder}")
        print("notes.csv / notes.jsonl 为完整记录；summary.json 对应各关键词与任务。")
        if sys.platform == "darwin" and sys.stdin.isatty() and not self.exit_requested:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["/usr/bin/open", str(folder)], check=False, timeout=10)
        return folder

    def collect_batch(self, arguments, *, batch_id=None):
        self.prepare_device()
        if self.exit_requested or self.cancel_requested:
            return
        print("\n开始依次采集关键词。Ctrl+C 暂停整个批次；菜单 7 可恢复。", flush=True)
        code, values = self.cli(*arguments)
        batch = batch_from(values)
        if batch is None:
            started = next((v for v in values if v.get("batch_id")), {})
            batch_id = batch_id or started.get("batch_id")
            if batch_id:
                _, values = self.cli("batch", "status", "--batch", batch_id, timeout=30)
                batch = batch_from(values)
        if batch is not None:
            self.show_batch(batch)
            self.export_batch(batch)
            print("已完成任务会保留；各关键词的观察数仍需人工核对不同笔记身份。")
        if code not in (0, 3) or batch is None:
            raise LauncherError(f"批次运行未正常结束，请从菜单 7 恢复。日志：{self.log_dir}")

    def batch_arguments(self):
        print("逐行填写关键词，空行结束（最多 20 个；第一个空行取消）：")
        keywords = []
        for index in range(20):
            value = input(f"关键词 {index + 1}：").strip()
            if not value:
                break
            if value in keywords:
                raise LauncherError("关键词重复，请重新填写")
            keywords.append(value)
        if not keywords:
            return None
        value = input("每个关键词的目标条数（回车默认 10）：").strip() or "10"
        self.validate_new_target(value)
        return ["batch", "run", "--device", self.device_id, "--limit", value,
                *[f"--keyword={keyword}" for keyword in keywords]]

    def validate_new_target(self, value):
        if not value.isascii() or not value.isdecimal():
            raise LauncherError("请输入 1 至 500 的整数")
        try:
            self.settings.new_task_policy(int(value))
        except ValueError as exc:
            raise LauncherError(str(exc)) from exc

    def menu(self):
        print("\n小红书手机采集\n")
        print("1  采集手机当前打开的图文笔记")
        print("2  输入关键词，自动搜索采集")
        print("3  恢复已有任务")
        print("4  查看任务 / 导出结果")
        print("5  检查数据库和手机连接")
        print("6  多个关键词，自动依次采集")
        print("7  恢复已有批次")
        print("8  查看批次 / 导出汇总")
        print("0  退出")
        choice = input("\n请选择：").strip()
        if choice == "0":
            return False
        if choice not in {"1", "2", "3", "4", "5", "6", "7", "8"}:
            print("请输入 0 至 8。")
            return True
        arguments = None
        batch_arguments = None
        if choice == "1":
            arguments = ["collect-current", "--device", self.device_id]
        elif choice == "2":
            keyword = input("关键词（回车取消）：").strip()
            if not keyword:
                return True
            value = input("采集目标条数（回车默认 10）：").strip() or "10"
            self.validate_new_target(value)
            arguments = ["run", "--device", self.device_id, "--keyword", keyword, "--limit", value]
        elif choice == "6":
            batch_arguments = self.batch_arguments()
            if batch_arguments is None:
                return True
        # Separate launcher exclusion from the CLI's actual DeviceLock.
        # Status/export stays available while another launcher is collecting.
        lock = contextlib.nullcontext() if choice in {"4", "8"} else DeviceLock(
            self.device_id, self.settings.state_dir / "launcher"
        )
        with lock:
            self.cancel_requested = False
            self.prepare_database()
            if arguments:
                self.collect(arguments)
            elif batch_arguments:
                self.collect_batch(batch_arguments)
            elif choice in {"7", "8"}:
                batch = self.pick_batch()
                if batch and choice == "7":
                    self.collect_batch(
                        ["batch", "resume", "--batch", batch["id"]], batch_id=batch["id"],
                    )
                elif batch:
                    self.show_batch(batch)
                    self.export_batch(batch)
            elif choice in {"3", "4"}:
                task = self.pick_task()
                if task and choice == "3":
                    self.collect(["resume", "--task", task["id"]], task_id=task["id"])
                elif task:
                    self.show_task(task)
                    self.export_task(task)
            elif choice == "5":
                self.prepare_device()
        return not self.exit_requested


def main():
    parser = argparse.ArgumentParser(description="双击启动小红书采集；无需输入终端命令。")
    parser.add_argument("--check", action="store_true", help="只准备数据库并诊断手机，不采集")
    parser.add_argument("--device", default="lab01")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        launcher = Launcher(device_id=args.device)
        if args.check:
            with DeviceLock(args.device, launcher.settings.state_dir / "launcher"):
                launcher.prepare_database()
                launcher.prepare_device()
            print(f"启动检查通过；未创建采集任务。日志：{launcher.log_dir}")
            return 0
        while not launcher.exit_requested:
            try:
                if not launcher.menu():
                    break
            except (LauncherError, LocalDatabaseError, DeviceError, OSError, ValueError,
                    subprocess.SubprocessError) as exc:
                if isinstance(exc, LauncherError):
                    print(f"\n{exc}")
                elif isinstance(exc, DeviceError):
                    print("\n手机正被另一进程使用，或设备锁不可用。请先查看原采集窗口。")
                else:
                    print(f"\n操作未完成（{type(exc).__name__}）。请检查本机配置、连接或运行占用。")
                print(f"日志目录：{launcher.log_dir}\n单任务用菜单 3，批次用菜单 7 恢复。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n已退出启动菜单。")
        return 0
    except Exception as exc:
        # Never echo raw connection/database exceptions containing credentials.
        print(f"启动未完成（{type(exc).__name__}）。请检查 config.local.toml 和本机接入配置。")
        if isinstance(exc, LauncherError):
            print(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
