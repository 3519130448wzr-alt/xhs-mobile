"""Read-only desktop projections; raw observations remain the source of truth."""

from pathlib import Path

from sqlalchemy import func, select

from xhs_mobile.batch_runner import ATTEMPT_END_REASONS
from xhs_mobile.domain import base_readability
from xhs_mobile.models import (
    Batch,
    BatchItem,
    Event,
    Observation,
    PolicyState,
    Task,
    TaskRetryGrant,
)
from xhs_mobile.repository import Repository, object_dict

FIELD_LABELS = {
    "title": "标题", "body": "正文", "author": "作者", "published_at": "发布时间",
    "likes": "点赞数", "like_count": "点赞数", "favorites": "收藏数", "collects": "收藏数",
    "collect_count": "收藏数", "comments": "评论数", "comment_count": "评论数",
    "tags": "标签", "note_url": "笔记链接", "url": "笔记链接", "canonical_url": "笔记链接",
    "note_id": "笔记标识", "shares": "分享数",
}
STATUS_LABELS = {
    "present": "已读取", "ok": "已读取", "not_readable": "无法读取",
    "not_displayed": "页面未展示", "low_quality": "识别质量待核对",
}
REASON_LABELS = {
    "empty_count": "页面未显示数值",
    "unparsed_count": "已保留显示原文，未换算数值",
    "noninteger_count": "数值无法可靠换算",
    "field_not_calibrated": "尚未适配此字段",
    "ambiguous_field_selector": "无法可靠确定内容位置",
    "selector_missing_or_empty": "页面未提供可读内容",
    "possible_ui_truncation_or_placeholder": "内容可能被截断或尚未加载",
    "explicit_title_absence_marker": "页面已明确标记未展示标题",
    "ocr_unavailable": "本地文字识别暂不可用",
    "tesseract_not_installed": "尚未安装本地文字识别工具",
    "ocr_region_out_of_bounds": "识别区域超出截图",
    "ocr_timeout": "文字识别超时",
    "tesseract_failed_check_local_language_packs": "文字识别失败，请检查本地语言包",
    "ocr_invalid_image": "截图无法识别",
    "ocr_invalid_tsv": "文字识别结果无法解析",
    "ocr_no_text": "识别区域没有可读文字",
    "ocr_visible_region_only": "仅识别截图可见部分",
    "ocr_low_confidence": "文字识别可信度不足",
    "platform_topics_unrecognized": "平台话题未能识别",
}
WARNING_LABELS = {
    "invalid_explicit_note_id": "笔记标识未通过校验",
    "conflicting_identity_evidence": "笔记身份信息不一致",
}
LEGACY_COMPLETENESS_WARNINGS = {"body_not_fully_visible", "body_not_confirmed_complete"}


class DesktopStore:
    def __init__(self, repository: Repository, state_dir: Path, device_id: str):
        self.repo, self.root, self.device_id = repository, state_dir, device_id

    def history(self, active: dict | None = None) -> list[dict]:
        """One transaction, no CLI calls, no phone calls and no state reconciliation writes."""
        with self.repo.sessions() as session:
            batches = list(session.scalars(select(Batch).where(Batch.device_id == self.device_id)))
            members = list(session.scalars(select(BatchItem).where(
                BatchItem.batch_id.in_([batch.id for batch in batches])
            )))
            tasks = list(session.scalars(select(Task).where(Task.device_id == self.device_id)))
            ids = [task.id for task in tasks]
            # A newer atomic batch can appear after the first SELECT under READ COMMITTED.
            # Do not temporarily display its newly visible children as standalone tasks.
            all_child_ids = set(session.scalars(select(BatchItem.task_id).where(
                BatchItem.task_id.in_(ids)
            )))
            policies = list(session.scalars(select(PolicyState)))
            read_credits = dict(session.execute(select(
                TaskRetryGrant.task_id, func.sum(TaskRetryGrant.credits),
            ).where(TaskRetryGrant.task_id.in_(ids), TaskRetryGrant.step == "capture").group_by(
                TaskRetryGrant.task_id,
            )).all())
            identities = list(session.execute(
                select(Observation.task_id, Observation.review_identity).where(
                    Observation.task_id.in_(ids), Observation.review_verdict == "accept",
                    Observation.review_identity.is_not(None),
                )
            ))
            confirmed = {task_id: set() for task_id in ids}
            for task_id, identity in identities:
                confirmed[task_id].add(identity)
            reliable = {task_id: set() for task_id in ids}
            for task_id, note_id in session.execute(select(
                Observation.task_id, Observation.note_id,
            ).where(Observation.task_id.in_(ids), Observation.note_id.is_not(None))):
                if note_id:
                    reliable[task_id].add(note_id)
            children = all_child_ids
            records = {}
            for task in tasks:
                scope = {"platform:xiaohongshu", f"session:{task.session_ref}",
                         f"device:{task.serial_hash}"}
                blocks = [p for p in policies if p.scope in scope]
                untils = [object_dict(p)["until"] for p in blocks if p.until]
                reason = task.stop_reason or ""
                finished = task.status == "collected_awaiting_review" or (
                    task.status == "partial" and reason in ATTEMPT_END_REASONS
                ) or reason == "connection:connection_recovery_exhausted"
                title = "当前笔记采集" if task.mode == "current" else task.keyword
                record = self._base(task, "task", title, active)
                failures = task.retry_counts.get("capture", 0)
                credits = read_credits.get(task.id, 0)
                read_streak = task.consecutive_read_failures
                progress_streak = task.consecutive_no_progress
                requires_read_check = (read_streak >= 10 or progress_streak >= 10
                                       or "capture: persistent retry budget exhausted" in reason)
                record.update(
                    observations=task.observation_count, eligible=task.eligible_count,
                    base_fields_readable=task.eligible_count,
                    reliable_identities=len(reliable[task.id]),
                    confirmed_identities=len(confirmed[task.id]), target=task.target,
                    can_resume=not finished and not record["active"],
                    requires_ack=any(p.status == "manual" for p in blocks),
                    requires_read_check=bool(requires_read_check and not finished),
                    read_anomaly={"consecutive_failures": read_streak,
                                  "consecutive_no_progress": progress_streak, "threshold": 10},
                    requires_read_retry=False,
                    read_retry={"failure_count": failures, "granted_credits": credits,
                                "available_attempts": None, "exhausted": False,
                                "mode": "consecutive", "threshold": 10},
                    cooldown_until=max(untils) if untils else None,
                )
                records[task.id] = record
            result = [item for key, item in records.items() if key not in children]
            for batch in batches:
                child_ids = [member.task_id for member in sorted(members, key=lambda m: m.position)
                             if member.batch_id == batch.id]
                child_items = [records[task_id] for task_id in child_ids]
                record = self._base(batch, "batch", "、".join(
                    item["title"] for item in child_items
                ), active)
                # A batch owns every child for the duration of the worker, but only one
                # child is currently running. Never report stale DB running as live.
                for item in child_items:
                    item["active"] = bool(record["active"] and active and (
                        active.get("task_id") == item["id"]
                    ))
                incomplete = [item for item in child_items if item["can_resume"] or item["active"]]
                untils = [item["cooldown_until"] for item in incomplete if item["cooldown_until"]]
                record.update(
                    tasks=child_items,
                    observations=sum(item["observations"] for item in child_items),
                    eligible=sum(item["eligible"] for item in child_items),
                    base_fields_readable=sum(item["eligible"] for item in child_items),
                    reliable_identities=len(set().union(*(reliable[i] for i in child_ids))),
                    confirmed_identities=len(set().union(*(confirmed[i] for i in child_ids))),
                    target=sum(item["target"] for item in child_items),
                    can_resume=bool(incomplete) and not record["active"],
                    requires_ack=bool(incomplete and incomplete[0]["requires_ack"]),
                    requires_read_retry=False,
                    requires_read_check=bool(incomplete and incomplete[0]["requires_read_check"]),
                    read_anomaly=incomplete[0]["read_anomaly"] if incomplete else None,
                    read_retry=incomplete[0]["read_retry"] if incomplete else None,
                    cooldown_until=max(untils) if untils else None,
                )
                result.append(record)
        return sorted(result, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    def _base(self, row, kind, title, active):
        values = object_dict(row)
        live = bool(active and active.get("kind") == kind and active.get("id") == row.id)
        return {
            "id": row.id, "kind": kind, "title": title,
            "status": row.status, "stop_reason": row.stop_reason,
            "created_at": values["created_at"], "updated_at": values["updated_at"],
            "active": live, "tasks": [], "export_path": self.latest_export(kind, row.id),
        }

    def latest_export(self, kind: str, identifier: str) -> str | None:
        folder = self.root / "exports"
        if kind == "batch":
            folder /= "batches"
        folder /= identifier
        if not folder.is_dir() or folder.is_symlink():
            return None
        for candidate in sorted(folder.iterdir(), reverse=True):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if ((candidate / "notes.csv").is_file() and (candidate / "notes.jsonl").is_file()
                    and not (candidate / "INCOMPLETE.txt").exists()):
                return str(candidate.resolve())
        return None

    def item(self, kind, identifier, active=None):
        if kind not in {"task", "batch"} or not isinstance(identifier, str):
            raise ValueError("请选择有效的任务")
        for item in self.history(active):
            if item["id"] == identifier and item["kind"] == kind:
                return item
            if kind == "task":
                for child in item["tasks"]:
                    if child["id"] == identifier:
                        return child
        raise ValueError("没有找到此设备的任务")

    def detail(self, kind, identifier, offset=0, limit=30, active=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("记录分页参数无效")
        item = self.item(kind, identifier, active)
        task_ids = [child["id"] for child in item["tasks"]] if kind == "batch" else [identifier]
        with self.repo.sessions() as session:
            predicate = Observation.task_id.in_(task_ids)
            total = session.scalar(select(func.count()).select_from(Observation).where(predicate))
            rows = list(session.scalars(select(Observation).where(predicate).order_by(
                Observation.captured_at, Observation.id
            ).offset(offset).limit(limit)))
            records = [self._record(row) for row in rows]
            failures = list(session.scalars(select(Event).where(
                Event.task_id.in_(task_ids), Event.kind == "failure_evidence",
            ).order_by(Event.created_at.desc(), Event.id.desc()).limit(3)))
            failure_paths = self.evidence_paths([
                event.detail["evidence"] for event in failures if "evidence" in event.detail
            ])
        next_offset = offset + len(records)
        return {"item": item, "records": records, "total": total,
                "failure_evidence_paths": failure_paths,
                "next_offset": next_offset if next_offset < total else None}

    def records(self, kind, identifier):
        item = self.item(kind, identifier)
        ids = [child["id"] for child in item["tasks"]] if kind == "batch" else [identifier]
        with self.repo.sessions() as session:
            return [object_dict(row) for row in session.scalars(select(Observation).where(
                Observation.task_id.in_(ids)
            ).order_by(Observation.captured_at, Observation.id))]

    def _record(self, row):
        fields = row.data.get("fields", {})
        readable, missing = base_readability(row.data)
        missing_labels = [FIELD_LABELS.get(name, "图文笔记页面") for name in missing]
        quality = ["基础字段可读" if readable else f"基础字段需核对：{'、'.join(missing_labels)}"]
        quality.append("正文完整性未自动判断，请人工核对")
        diagnostics = []
        for warning in row.data.get("warnings", []):
            code = str(warning)
            diagnostics.append(code)
            if code in LEGACY_COMPLETENESS_WARNINGS:
                continue
            quality.append(
                "页面不是图文笔记详情" if code.startswith("not_image_text_detail:")
                else WARNING_LABELS.get(code, "需人工核对")
            )
        for name, field in fields.items():
            if name in {"published_at", "tags"}:
                # These have separate, explicit preview rows instead of a long generic warning.
                if field.get("reason"):
                    diagnostics.append(f"{name}:{field.get('status')}:{field['reason']}")
                continue
            status, reason = field.get("status"), field.get("reason")
            if status not in {"ok", "present"} or field.get("approximate") or reason:
                diagnostics.append(f"{name}:{status}:{reason or ''}")
                description = STATUS_LABELS.get(status, "需人工核对")
                if field.get("approximate"):
                    description += "，近似值"
                if reason and reason != "explicit_title_absence_marker":
                    description += f"（{REASON_LABELS.get(reason, '需人工核对')}）"
                quality.append(f"{FIELD_LABELS.get(name, '其他字段')}：{description}")
        page_time = fields.get("published_at", {})
        time_kind = row.data.get("time_kind", "not_readable")
        if "编辑于" in str(page_time.get("raw") or ""):
            time_kind = "edited"
        topics = row.data.get("topics", []) if row.data.get("topics_status") == "confirmed" else []
        topics = list(dict.fromkeys(value for value in topics if isinstance(value, str) and value))
        return {
            "id": row.id, "title": fields.get("title", {}).get("raw"),
            "author": fields.get("author", {}).get("raw"),
            "body": fields.get("body", {}).get("raw"), "eligible": row.eligible,
            "base_fields_readable": readable, "missing_base_fields": missing,
            "title_status": fields.get("title", {}).get("status", "not_readable"),
            "completeness_status": "not_assessed",
            "page_time": page_time.get("raw"), "time_kind": time_kind,
            "time_label": {"edited": "编辑时间", "published": "页面发布时间"}.get(
                time_kind, "页面时间",
            ),
            "time_status": page_time.get("status", "not_readable"),
            "time_status_label": STATUS_LABELS.get(page_time.get("status"), "未读取"),
            "time_evidence_ref": page_time.get("evidence_ref"),
            "topics": topics, "topics_status": "confirmed" if topics else "unrecognized",
            "topic_sources": row.data.get("topic_sources", []) if topics else [],
            "identity_status": "verified" if row.note_id else "unverified",
            "note_id": row.note_id, "canonical_url": row.canonical_url,
            "quality": "；".join(dict.fromkeys(quality)),
            "quality_diagnostics": diagnostics,
            "evidence_paths": self.evidence_paths(row.evidence),
        }

    def evidence_paths(self, manifests):
        evidence = []
        root = (self.root / "evidence").resolve()
        for index, manifest in enumerate(manifests, 1):
            for kind, label in (("png", "截图"), ("xml", "UI 树")):
                relative = manifest.get("files", {}).get(kind, {}).get("path")
                if not isinstance(relative, str) or Path(relative).is_absolute():
                    continue
                path = root / relative
                if ".." in Path(relative).parts or not path.resolve().is_relative_to(root):
                    continue
                if path.is_file() and not path.is_symlink():
                    evidence.append({"label": f"{label} {index}", "path": str(path.resolve())})
        return evidence
