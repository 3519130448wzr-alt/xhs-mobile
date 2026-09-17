"""SYNTHETIC quality evidence. No records in this module are real collection output."""

import csv
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from xhs_mobile.domain import FieldValue, Snapshot, UIState, base_readability
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.exports import export_records
from xhs_mobile.models import Base, Observation, QualityAssessmentAudit, Task
from xhs_mobile.parser import page_time_kind, parse_note
from xhs_mobile.profile import FieldRule, Selector, load_profile
from xhs_mobile.reassessment import _same_observed_text, reassess_history
from xhs_mobile.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return load_profile(FIXTURES / "synthetic_profile.toml", require_verified=False)


def snapshot(time="编辑于昨天 22:10 上海"):
    xml = (FIXTURES / "synthetic_detail.xml").read_text().replace(
        "</hierarchy>", f'<node resource-id="synthetic/published-at" text="{time}"/></hierarchy>',
    )
    return Snapshot(xml=xml, png=b"SYNTHETIC opaque screenshot fixture",
                    metadata={"source_kind": "synthetic", "app_version": "SYNTHETIC-1",
                              "resolution": [1080, 1920]})


@pytest.mark.parametrize("raw,kind", [
    ("编辑于08-06浙江", "edited"), ("今天下午7:09广东", "published"),
    ("06-18山东", "published"), (None, "not_readable"),
])
def test_page_time_is_original_text_not_normalized(raw, kind):
    value = FieldValue(raw=raw, status="present" if raw else "not_readable")
    assert page_time_kind(value) == kind
    assert value.raw == raw and value.normalized is None


def test_time_selector_avoids_body_and_comment_dates_and_checks_uniqueness(profile):
    profile.fields["published_at"] = FieldRule(
        many=False, selector=Selector(class_name="date", parent=Selector(class_name="note-meta")),
    )
    snap = snapshot().xml.replace('</hierarchy>',
        '<node class="note-meta" index="2"><node class="date" text="昨天广东"/></node>'
        '<node class="comment"><node class="date" text="2020-01-01"/></node></hierarchy>')
    observed = snapshot()
    observed.xml = snap
    note = parse_note(observed, profile)
    assert note.fields["published_at"].raw == "昨天广东"
    observed.xml = observed.xml.replace('</hierarchy>',
        '<node class="note-meta"><node class="date" text="不同日期"/></node></hierarchy>')
    assert parse_note(observed, profile).fields["published_at"].reason == "ambiguous_field_selector"


def test_platform_topic_requires_explicit_calibration_and_deduplicates_components(profile):
    observed = snapshot()
    observed.xml = observed.xml.replace("SYNTHETIC 正文，保留原文。", "SYNTHETIC #普通井号内容")
    profile.fields["tags"].platform_topic = False
    note = parse_note(observed, profile)
    assert note.fields["tags"].raw is None and note.topics == []
    assert note.topics_status == "unrecognized"
    assert "#普通井号内容" in note.fields["body"].raw
    profile.fields["tags"].platform_topic = True
    observed.xml = observed.xml.replace('</hierarchy>',
        '<node resource-id="synthetic/tag" text="#synthetic"/></hierarchy>')
    note = parse_note(observed, profile)
    assert note.topics == ["#synthetic", "#测试"]
    assert note.topics_status == "confirmed"
    assert [item["name"] for item in note.topic_sources] == note.topics


def test_readability_exact_missing_fields_and_unknown_completeness(profile):
    note = parse_note(snapshot(), profile)
    assert note.body_complete is None and note.completeness_status == "not_assessed"
    for field in ("title", "body", "author"):
        data = note.to_dict()
        data["fields"][field]["raw"] = None
        assert base_readability(data) == (False, [field])
    data["fields"]["author"]["raw"] = "SYNTHETIC 作者"
    data["fields"]["title"].update(raw=None, status="not_displayed")
    assert base_readability(data) == (True, [])


def test_navigation_state_has_no_screenshot():
    state = UIState(xml="<hierarchy synthetic='true'/>")
    assert not hasattr(state, "png")


@pytest.fixture
def history(tmp_path, profile):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic-history.db'}")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    task = repo.create_task(device_id="synthetic", serial="SYNTHETIC", session_ref="synthetic",
                            keyword="SYNTHETIC", target=1, policy={})
    run = repo.start_run(task.id, "synthetic-profile")
    store = EvidenceStore(tmp_path / "evidence")
    observed = snapshot()
    manifest = store.save(observed, device_id="synthetic", run_id=run, label="detail_initial")
    note = parse_note(observed, profile)
    note.note_id = note.canonical_url = note.identity_source = None
    obs_id, _ = repo.save_note(observation_id=str(uuid4()), task_id=task.id, run_id=run,
                              note=note, snapshot=observed, evidence=[manifest])
    repo.finish(task.id, run, "paused", "user_pause")
    with repo.sessions.begin() as session:
        row = session.get(Observation, obs_id)
        data = deepcopy(row.data)
        for name in ("quality_revision", "completeness_status", "identity_status"):
            data.pop(name, None)
        data["fields"]["published_at"] = {"raw": None, "status": "not_readable"}
        data["body_complete"] = False
        data["warnings"] = ["body_not_fully_visible", "body_not_confirmed_complete"]
        row.data, row.eligible, row.schema_version = data, False, 1
        task_row = session.get(Task, task.id)
        task_row.eligible_count = 0
        task_row.detail_visits, task_row.list_swipes = 91, 47
        task_row.retry_counts = {"capture": 31, "connection:attempt": 2}
    yield repo, store, task.id, obs_id
    engine.dispose()


def test_history_dry_run_then_atomic_idempotent_apply_preserves_raw_and_budget(history, profile):
    repo, store, task_id, obs_id = history
    before = repo.observations(task_id)[0]
    task_before = repo.task(task_id)
    kwargs = {"profile": profile, "evidence_root": store.root}
    planned = reassess_history(repo.engine, **kwargs)
    assert planned["eligible_before"] == 0 and planned["eligible_after"] == 1
    assert planned["time_before"] == 0 and planned["time_after"] == 1
    assert repo.observations(task_id)[0] == before
    actual = reassess_history(repo.engine, **kwargs, dry_run=False)
    assert actual["changed"] == 1
    after = repo.observations(task_id)[0]
    for key in ("title", "body", "author"):
        assert after["data"]["fields"][key]["raw"] == before["data"]["fields"][key]["raw"]
    for key in ("evidence", "note_id", "captured_at", "review_verdict", "fingerprint"):
        assert after[key] == before[key]
    assert after["data"]["body_complete"] is None
    assert after["data"]["time_kind"] == "edited"
    assert after["data"]["fields"]["published_at"]["evidence_ref"]
    assert after["data"]["topics_status"] == "unrecognized"
    assert after["data"]["identity_status"] == "unverified"
    task_after = repo.task(task_id)
    for key in ("target", "policy", "detail_visits", "list_swipes", "retry_counts", "status"):
        assert getattr(task_after, key) == getattr(task_before, key)
    assert task_after.eligible_count == 1 and task_after.status == "paused"
    assert reassess_history(repo.engine, **kwargs, dry_run=False)["changed"] == 0
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QualityAssessmentAudit)) == 2
        audit = session.scalar(select(QualityAssessmentAudit).where(
            QualityAssessmentAudit.entity_id == obs_id))
        assert audit.before["data"]["body_complete"] is False


@pytest.mark.parametrize("previous,current,old_method,new_method,expected", [
    ("SYNTHETIC 🥕", "SYNTHETIC ..", "ui_objinfo", "ui", True),
    ("SYNTHETIC ..", "SYNTHETIC 🥕", "ui", "ui_objinfo", True),
    ("SYNTHETIC 🥕", "SYNTHETIC ..", "ui", "ui", False),
    ("SYNTHETIC 🥕", "SYNTHETIC 🐰", "ui_objinfo", "ui_objinfo", False),
    ("SYNTHETIC A🥕", "SYNTHETIC B..", "ui_objinfo", "ui", False),
])
def test_same_visit_sanitizer_comparison_requires_verified_source(
    previous, current, old_method, new_method, expected,
):
    assert _same_observed_text(
        {"raw": previous, "method": old_method},
        FieldValue(raw=current, status="present", method=new_method),
    ) is expected


def test_history_preserves_crlf_bytes_for_verified_ui_readback(history, profile, monkeypatch):
    from xhs_mobile import reassessment

    repo, store, task_id, obs_id = history
    before = repo.observations(task_id)[0]
    observed = snapshot()
    observed.xml = observed.xml.replace("\n", "\r\n")
    manifest = store.save(observed, device_id="synthetic", run_id=before["run_id"],
                          label="detail_initial")
    with repo.sessions.begin() as session:
        session.get(Observation, obs_id).evidence = [manifest]
    calls = []

    def exact_parser(restored, profile):
        assert restored.xml.encode("utf-8") == observed.xml.encode("utf-8")
        calls.append(True)
        return parse_note(restored, profile)

    monkeypatch.setattr(reassessment, "parse_note", exact_parser)
    result = reassess_history(repo.engine, profile=profile, evidence_root=store.root)
    assert calls and result["time_after"] == 1


def test_history_evidence_corruption_never_creates_a_time(history, profile):
    repo, store, task_id, _ = history
    before = repo.observations(task_id)[0]
    path = store.root / before["evidence"][0]["files"]["xml"]["path"]
    path.write_text("SYNTHETIC tampered evidence")
    result = reassess_history(repo.engine, profile=profile, evidence_root=store.root, dry_run=False)
    assert result["time_after"] == 0 and result["issues"]
    assert repo.observations(task_id)[0]["data"]["time_kind"] == "not_readable"


def test_history_transaction_failure_rolls_back_audit_and_derived_count(history, profile):
    repo, store, task_id, _ = history
    before = repo.observations(task_id)[0]

    def crash(session):
        raise RuntimeError("SYNTHETIC crash before commit")

    event.listen(Session, "before_commit", crash)
    try:
        with pytest.raises(RuntimeError, match="SYNTHETIC"):
            reassess_history(repo.engine, profile=profile, evidence_root=store.root, dry_run=False)
    finally:
        event.remove(Session, "before_commit", crash)
    assert repo.observations(task_id)[0] == before
    assert repo.task(task_id).eligible_count == 0
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QualityAssessmentAudit)) == 0


def test_history_refuses_apply_while_collector_running(history, profile):
    repo, store, task_id, _ = history
    repo.start_run(task_id, "synthetic-active")
    with pytest.raises(ValueError, match="Stop collection"):
        reassess_history(repo.engine, profile=profile, evidence_root=store.root, dry_run=False)


def test_export_time_topics_identity_and_completeness_are_independent(tmp_path, profile):
    note = parse_note(snapshot(), profile)
    records = [{"data": note.to_dict(), "eligible": note.eligible, "note_id": note.note_id}]
    path = tmp_path / "synthetic.csv"
    export_records(records, path, "csv")
    with path.open() as handle:
        row = next(csv.DictReader(handle))
    assert row["body_complete"] == ""
    assert row["completeness_status"] == "not_assessed" and row["base_readable"] == "true"
    assert row["time_kind"] == "edited"
    assert row["field.published_at.raw"] == "编辑于昨天 22:10 上海"
    assert json.loads(row["topics_json"]) == ["#synthetic", "#测试"]
    assert row["identity_status"] == "verified"
