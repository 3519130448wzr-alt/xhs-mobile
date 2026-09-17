"""Explicit operator entry points. Importing this module never contacts Android."""

import json
import signal
from datetime import datetime
from functools import wraps
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from xhs_mobile import __version__
from xhs_mobile.config import Settings, load_settings
from xhs_mobile.connection import ConnectionManager
from xhs_mobile.control import check_desktop_parent, desktop_controlled, execution_stop
from xhs_mobile.device import AndroidDevice, adb_devices, dependency_report
from xhs_mobile.domain import DeviceError, EvidenceError, PageError, Snapshot
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.locking import DeviceLock
from xhs_mobile.logging import journal
from xhs_mobile.profile import classify, load_profile
from xhs_mobile.repository import Repository, serial_hash
from xhs_mobile.runner import Runner

app = typer.Typer(
    help="5507 手机小红书采集：所有实机操作由实验室显式启动。",
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="数据库迁移。")
app.add_typer(db_app, name="db")
batch_app = typer.Typer(help="单台手机的持久多关键词批次。")
app.add_typer(batch_app, name="batch")


def emit(value: dict) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def guarded(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SQLAlchemyError as exc:
            # SQL errors can contain connection URLs and row data: do not echo them.
            emit(
                {
                    "ok": False,
                    "error": "database_error",
                    "type": type(exc).__name__,
                    "next": "检查数据库连接及迁移；未可靠提交的数据不会计为成果。",
                }
            )
            raise typer.Exit(2) from None
        except (ValueError, OSError, ValidationError, DeviceError, PageError, EvidenceError) as exc:
            emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
            raise typer.Exit(2) from None

    return wrapper


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option(help="本地TOML配置；路径相对配置文件解析。")] = Path(
        "config.local.toml"
    ),
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
):
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    ctx.obj = config


def settings_for(ctx: typer.Context) -> Settings:
    path = ctx.obj
    if not path.is_file():
        raise ValueError(f"未找到 {path}；先复制 config.example.toml 为 config.local.toml")
    return load_settings(path)


def device_for(settings: Settings, device_id: str) -> AndroidDevice:
    config = settings.device(device_id)
    return AndroidDevice(
        config.serial(), config.app_package, settings.adb_path, settings.policy.page_timeout
    )


def connection_options(settings: Settings, repo: Repository, task_id: str, stopped) -> dict:
    """A collector owns repair and can cancel it through either pause channel."""
    task = repo.task(task_id)
    manager = ConnectionManager(
        settings,
        task.device_id,
        stop_requested=lambda: stopped() or repo.task(task_id).pause_requested,
        progress=lambda update: emit(
            {**update, "event": "connection_progress", "task_id": task_id}
        ),
    )
    if not manager.auto_enabled:
        return {}
    # The caller holds the task's device lock, not an arbitrary serial found in
    # a changed connection record. Verify before even a read-only transport probe.
    if serial_hash(manager.serial) != task.serial_hash:
        raise ValueError("自动连接记录与任务绑定的设备不一致；请检查连接设置，不能迁移任务设备")
    return {"connection": manager}


@app.command()
@guarded
def doctor(
    ctx: typer.Context,
    device: Annotated[str, typer.Option(help="配置中的明确设备标识。")],
    check_input: Annotated[
        bool, typer.Option(help="在已人工聚焦的搜索输入框测试中文；不提交搜索。")
    ] = False,
    input_prefix: Annotated[
        str, typer.Option(help="真实UI样本确认的输入回读前缀；仅可与--check-input同用。")
    ] = "",
):
    prefix_source = ctx.get_parameter_source("input_prefix")
    if not check_input and (
        input_prefix or (prefix_source is not None and prefix_source.name == "COMMANDLINE")
    ):
        raise ValueError("--input-prefix requires --check-input")
    settings = settings_for(ctx)
    report = {
        "dependencies": dependency_report(settings.adb_path),
        "device_id": device,
        "real_device_acceptance": "not_tested",
    }
    config = settings.device(device)
    try:
        serial = config.serial()
    except ValueError as exc:
        report.update(ok=False, configuration_error=str(exc))
        emit(report)
        raise typer.Exit(2) from None
    adapter = device_for(settings, device)
    with DeviceLock(serial, settings.state_dir):
        try:
            report["adb_devices"] = adb_devices(settings.adb_path)
        except DeviceError as exc:
            report["adb_error"] = str(exc)
        report["health"] = adapter.health()
        if check_input and report["health"].get("ok"):
            report["input_check"] = adapter.check_input(readback_prefix=input_prefix)
    report["ok"] = bool(report["health"].get("ok"))
    if check_input:
        report["ok"] = report["ok"] and bool(report.get("input_check", {}).get("ok"))
    journal(settings.state_dir, "doctor", {"device_id": device, "ok": report["ok"]})
    emit(report)
    if not report["ok"]:
        raise typer.Exit(2)


@app.command()
@guarded
def snapshot(
    ctx: typer.Context,
    device: Annotated[str, typer.Option()],
    label: Annotated[str, typer.Option()] = "manual_snapshot",
):
    settings = settings_for(ctx)
    adapter = device_for(settings, device)
    with DeviceLock(adapter.serial, settings.state_dir):
        snap = adapter.capture()
        manifest = EvidenceStore(settings.state_dir / "evidence").save(
            snap,
            device_id=device,
            run_id="diagnostic",
            label=label,
        )
    emit({"ok": True, "evidence_root": str(settings.state_dir / "evidence"), "evidence": manifest})


@app.command("profile-check")
@guarded
def profile_check(
    profile: Annotated[Path, typer.Option(help="TOML规则；默认要求已完成校准。")],
    sample: Annotated[
        Path | None, typer.Option(help="snapshot生成的证据目录，含manifest.json。")
    ] = None,
    draft: Annotated[
        bool, typer.Option(help="只读检查未完成的草稿；不验证生产可用性或修改verified。")
    ] = False,
):
    loaded = load_profile(profile, require_verified=not draft)
    snap = None
    if sample:
        manifest = json.loads((sample / "manifest.json").read_text())
        store = EvidenceStore(sample.parent)
        store.verify(manifest)
        if sample.name != manifest["evidence_id"]:
            raise EvidenceError("Sample directory does not match the verified evidence ID")
        verified_sample = store.root / manifest["evidence_id"]
        snap = Snapshot(
            xml=(verified_sample / "ui.xml").read_bytes().decode("utf-8"),
            png=(verified_sample / "screen.png").read_bytes(),
            metadata=manifest["metadata"],
            captured_at=datetime.fromisoformat(manifest["captured_at"]),
        )
    if draft:
        from xhs_mobile.inspection import inspect_draft

        emit(inspect_draft(loaded, snap))
        return
    report = {
        "ok": True,
        "profile": loaded.name,
        "verified": loaded.verified,
        "app_package": loaded.app_package,
        "app_version": loaded.app_version,
        "note": "配置及样本检查不替代30篇实机验收",
    }
    if snap is not None:
        from dataclasses import asdict

        from xhs_mobile.ocr import LocalOCR
        from xhs_mobile.parser import candidates, parse_note
        from xhs_mobile.profile import locate

        report["page"] = classify(snap, loaded)
        report["ok"] = report["page"] != "unknown"
        report["action_readability"] = {name: locate(snap, loaded, name) for name in loaded.actions}
        report["candidate_count"] = (
            len(candidates(snap, loaded)) if report["page"] == "results" else 0
        )
        if report["page"] == "detail":
            parsed = parse_note(snap, loaded, LocalOCR())
            report["field_readability"] = {
                name: asdict(value) for name, value in parsed.fields.items()
            }
            report["body_complete"] = parsed.body_complete
            report["eligible"] = parsed.eligible
            report["identity_source"] = parsed.identity_source
    emit(report)
    if not report["ok"]:
        raise typer.Exit(2)


@db_app.command("upgrade")
@guarded
def db_upgrade(ctx: typer.Context):
    settings = settings_for(ctx)
    configuration = Config()
    configuration.set_main_option("script_location", str(files("xhs_mobile") / "migrations"))
    configuration.attributes["database_url"] = settings.database_url()
    command.upgrade(configuration, "head")
    emit({"ok": True, "schema": "head"})


def execute_task(
    settings: Settings, repo: Repository, task_id: str, *, acknowledge=False,
    retry_read_request_id: str | None = None,
) -> None:
    task = repo.task(task_id)
    adapter = device_for(settings, task.device_id)
    current = settings.device(task.device_id)
    if serial_hash(adapter.serial) != task.serial_hash or current.session_ref != task.session_ref:
        raise ValueError("任务绑定的设备或会话已改变；不能迁移受限任务继续运行")
    profile = load_profile(settings.profile_path)
    if current.app_package != profile.app_package:
        raise ValueError("配置包名与已校准页面规则不一致")
    stop, stopped = execution_stop()
    previous_handlers = {}
    with DeviceLock(adapter.serial, settings.state_dir):
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            journal(
                settings.state_dir, "run_started", {"task_id": task_id, "device_id": task.device_id}
            )
            emit({"event": "run_started", "task_id": task_id, "device_id": task.device_id})
            runner = Runner(
                repository=repo,
                device=adapter,
                evidence=EvidenceStore(settings.state_dir / "evidence"),
                profile=profile,
                stop_requested=stopped,
                **connection_options(settings, repo, task_id, stopped),
            )
            if retry_read_request_id:
                grant = runner.check_and_grant_read_retry(
                    task_id, retry_read_request_id, acknowledge=acknowledge,
                )
                emit({"event": "read_retry_checked", "task_id": task_id, **grant})
            result = runner.execute(task_id, acknowledge=acknowledge)
            journal(
                settings.state_dir,
                "run_finished",
                {
                    "task_id": task_id,
                    "status": result["tasks"][0]["status"],
                },
            )
            emit(result)
            if result["tasks"][0]["status"] != "collected_awaiting_review":
                raise typer.Exit(3)
        except (SQLAlchemyError, EvidenceError) as exc:
            journal(
                settings.state_dir,
                "emergency_stop",
                {
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                    "message": "停止操作；需要检查存储，恢复时读取最后已提交检查点。",
                },
            )
            raise
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def create_and_run(ctx, device: str, keyword: str, limit: int, mode: str) -> None:
    settings = settings_for(ctx)
    config = settings.device(device)
    # Fail before creating work or connecting Android when no real calibration exists.
    profile = load_profile(settings.profile_path)
    if not config.app_package or config.app_package != profile.app_package:
        raise ValueError("先确认设备App包名并完成匹配的页面校准")
    repo = Repository.connect(settings.database_url())
    check_desktop_parent()
    task = repo.create_task(
        device_id=device,
        serial=config.serial(),
        session_ref=config.session_ref,
        keyword=keyword,
        target=limit,
        policy=settings.new_task_policy(limit).model_dump(),
        mode=mode,
    )
    # The desktop can recover this durable task even if setup fails before a run.
    emit({"event": "task_created", "task_id": task.id, "device_id": device})
    execute_task(settings, repo, task.id)


@app.command("run")
@guarded
@desktop_controlled
def run_command(
    ctx: typer.Context,
    device: Annotated[str, typer.Option()],
    keyword: Annotated[str, typer.Option()],
    limit: Annotated[int, typer.Option(min=1, max=500)] = 10,
):
    create_and_run(ctx, device, keyword, limit, "search")


@app.command("collect-current")
@guarded
@desktop_controlled
def collect_current(ctx: typer.Context, device: Annotated[str, typer.Option()]):
    create_and_run(ctx, device, "(current detail)", 1, "current")


@app.command()
@guarded
def status(ctx: typer.Context, task: Annotated[str | None, typer.Option()] = None):
    settings = settings_for(ctx)
    emit(Repository.connect(settings.database_url()).status(task))


@app.command()
@guarded
def pause(ctx: typer.Context, task: Annotated[str, typer.Option()]):
    settings = settings_for(ctx)
    Repository.connect(settings.database_url()).request_pause(task)
    emit(
        {
            "ok": True,
            "task_id": task,
            "pause_requested": True,
            "note": "当前动作结束后的安全边界暂停；不强杀设备命令。",
        }
    )


@app.command()
@guarded
@desktop_controlled
def resume(
    ctx: typer.Context,
    task: Annotated[str, typer.Option()],
    acknowledge: Annotated[
        bool, typer.Option(help="实验室已正常处理登录/验证/未知页；程序仍核验恢复页。")
    ] = False,
    limit: Annotated[
        int | None, typer.Option(
            min=1, max=500, help="提高合格观察目标以补采；不清零任何已用预算。"
        )
    ] = None,
    retry_read_request_id: Annotated[
        UUID | None, typer.Option(help="兼容旧参数：检查后继续，不再追加或要求读取额度。")
    ] = None,
):
    settings = settings_for(ctx)
    repo = Repository.connect(settings.database_url())
    check_desktop_parent()
    if limit is not None:
        repo.raise_target(task, limit)
    execute_task(settings, repo, task, acknowledge=acknowledge,
                 retry_read_request_id=(str(retry_read_request_id)
                                        if retry_read_request_id else None))


@app.command("export")
@guarded
def export_command(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option()],
    format: Annotated[str, typer.Option()] = "jsonl",
    task: Annotated[str | None, typer.Option()] = None,
):
    from xhs_mobile.exports import export_records

    settings = settings_for(ctx)
    repo = Repository.connect(settings.database_url())
    if task:
        repo.task(task)
    emit(export_records(repo.observations(task), output, format))


@app.command()
@guarded
def review(
    ctx: typer.Context,
    observation: Annotated[str, typer.Option()],
    reviewer: Annotated[str, typer.Option()],
    verdict: Annotated[str, typer.Option()],
    identity: Annotated[str | None, typer.Option()] = None,
):
    settings = settings_for(ctx)
    repo = Repository.connect(settings.database_url())
    rows = [row for row in repo.observations() if row["id"] == observation]
    if not rows:
        raise ValueError("observation not found")
    if verdict == "accept":
        for manifest in rows[0]["evidence"]:
            EvidenceStore(settings.state_dir / "evidence").verify(manifest)
    repo.review(observation, verdict=verdict, reviewer=reviewer, identity=identity)
    emit({"ok": True, "observation_id": observation, "verdict": verdict})


@app.command()
@guarded
def acceptance(ctx: typer.Context, tasks: Annotated[list[str], typer.Option()]):
    from xhs_mobile.acceptance import acceptance_report

    settings = settings_for(ctx)
    repo = Repository.connect(settings.database_url())
    report = acceptance_report(repo, tasks, EvidenceStore(settings.state_dir / "evidence"))
    emit(report)
    if not report["passed"]:
        raise typer.Exit(3)


def execute_batch(
    settings: Settings, repo: Repository, batch_id: str, *, acknowledge=False,
    retry_read_request_id: str | None = None,
):
    from xhs_mobile.batch_runner import BatchRunner
    from xhs_mobile.batches import BatchRepository

    batches = BatchRepository(repo)
    batch = batches.batch(batch_id)
    config = settings.device(batch.device_id)
    adapter = device_for(settings, batch.device_id)
    if serial_hash(adapter.serial) != batch.serial_hash or config.session_ref != batch.session_ref:
        raise ValueError("批次绑定的设备或会话已改变；不能迁移受限批次")
    profile = load_profile(settings.profile_path)
    if config.app_package != profile.app_package:
        raise ValueError("配置包名与已校准页面规则不一致")
    for task_id in batches.task_ids(batch_id):
        task = repo.task(task_id)
        if (task.device_id, task.serial_hash, task.session_ref) != (
            batch.device_id, batch.serial_hash, batch.session_ref,
        ):
            raise ValueError("批次包含设备或会话不一致的任务")
    stop, stopped = execution_stop()
    previous = {}
    last_action = None
    read_retry_pending = retry_read_request_id

    def execute_child(task_id, stopped, child_acknowledge):
        nonlocal last_action, read_retry_pending
        emit({"event": "batch_task_started", "batch_id": batch_id, "task_id": task_id,
              "keyword": repo.task(task_id).keyword})
        runner = Runner(
            repository=repo, device=adapter,
            evidence=EvidenceStore(settings.state_dir / "evidence"), profile=profile,
            stop_requested=stopped,
            **connection_options(settings, repo, task_id, stopped),
        )
        # Preserve the minimum interval across keyword boundaries in this process.
        runner.last_action = last_action
        try:
            if read_retry_pending:
                grant = runner.check_and_grant_read_retry(
                    task_id, read_retry_pending, acknowledge=child_acknowledge,
                )
                read_retry_pending = None
                emit({"event": "read_retry_checked", "task_id": task_id, **grant})
            runner.execute(task_id, acknowledge=child_acknowledge)
        finally:
            last_action = runner.last_action

    def finished(task_id):
        task = repo.task(task_id)
        emit({"event": "batch_task_finished", "batch_id": batch_id, "task_id": task_id,
              "status": task.status, "observations": task.observation_count,
              "eligible_observations": task.eligible_count})

    with DeviceLock(adapter.serial, settings.state_dir):
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            emit({"event": "batch_started", "batch_id": batch_id, "device_id": batch.device_id})
            report = BatchRunner(
                repository=repo, batches=batches, execute_task=execute_child,
                stop_requested=stopped, task_finished=finished,
            ).execute(batch_id, acknowledge=acknowledge)
            emit(report)
            if report["batches"][0]["status"] != "collected_awaiting_review":
                raise typer.Exit(3)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


@batch_app.command("run")
@guarded
@desktop_controlled
def batch_run(
    ctx: typer.Context,
    device: Annotated[str, typer.Option()],
    keyword: Annotated[list[str], typer.Option("--keyword", help="重复此参数添加关键词。")],
    limit: Annotated[int, typer.Option(min=1, max=500)] = 10,
):
    from xhs_mobile.batches import BatchRepository

    settings = settings_for(ctx)
    config = settings.device(device)
    profile = load_profile(settings.profile_path)
    if not config.app_package or config.app_package != profile.app_package:
        raise ValueError("先完成匹配的页面校准")
    serial = config.serial()
    repo = Repository.connect(settings.database_url())
    check_desktop_parent()
    batch_id = BatchRepository(repo).create_batch(
        device_id=device, serial=serial, session_ref=config.session_ref,
        keywords=keyword, target=limit, policy=settings.new_task_policy(limit).model_dump(),
    )
    # Emit the durable ID even when the phone is already busy or disconnected.
    emit({"event": "batch_created", "batch_id": batch_id})
    execute_batch(settings, repo, batch_id)


@batch_app.command("resume")
@guarded
@desktop_controlled
def batch_resume(
    ctx: typer.Context,
    batch: Annotated[str, typer.Option()],
    acknowledge: Annotated[
        bool, typer.Option(help="实验室已正常处理当前阻塞；只确认首个待恢复任务。")
    ] = False,
    retry_read_request_id: Annotated[
        UUID | None, typer.Option(help="兼容旧参数：检查首个待恢复任务，不再追加读取额度。")
    ] = None,
):
    settings = settings_for(ctx)
    execute_batch(settings, Repository.connect(settings.database_url()), batch,
                  acknowledge=acknowledge,
                  retry_read_request_id=(str(retry_read_request_id)
                                         if retry_read_request_id else None))


@batch_app.command("status")
@guarded
def batch_status(ctx: typer.Context, batch: Annotated[str | None, typer.Option()] = None):
    from xhs_mobile.batches import BatchRepository

    settings = settings_for(ctx)
    emit(BatchRepository(Repository.connect(settings.database_url())).status(batch))


@batch_app.command("pause")
@guarded
def batch_pause(ctx: typer.Context, batch: Annotated[str, typer.Option()]):
    from xhs_mobile.batches import BatchRepository

    settings = settings_for(ctx)
    BatchRepository(Repository.connect(settings.database_url())).request_pause(batch)
    emit({"ok": True, "batch_id": batch, "pause_requested": True})


@batch_app.command("export")
@guarded
def batch_export(
    ctx: typer.Context,
    batch: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    format: Annotated[str, typer.Option()] = "jsonl",
):
    from sqlalchemy import select

    from xhs_mobile.batches import BatchRepository
    from xhs_mobile.exports import export_records
    from xhs_mobile.models import Observation
    from xhs_mobile.repository import object_dict

    settings = settings_for(ctx)
    repo = Repository.connect(settings.database_url())
    batches = BatchRepository(repo)
    report = batches.status(batch)["batches"][0]
    ids = [task["id"] for task in report["tasks"]]
    # One SELECT gives the whole batch a consistent statement-level DB snapshot.
    with repo.sessions() as session:
        rows = list(session.scalars(
            select(Observation).where(Observation.task_id.in_(ids))
            .order_by(Observation.captured_at, Observation.id)
        ))
        records = [object_dict(row) for row in rows]
    if format == "bundle":
        from xhs_mobile.batch_exports import export_bundle

        emit(export_bundle(records, report, output))
    else:
        emit(export_records(records, output, format))


if __name__ == "__main__":
    app()
