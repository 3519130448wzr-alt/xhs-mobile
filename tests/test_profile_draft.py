"""Draft inspection uses only explicitly SYNTHETIC local evidence, never a phone or DB."""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xhs_mobile import cli
from xhs_mobile.domain import Snapshot
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.profile import load_profile

runner = CliRunner()


@pytest.fixture(autouse=True)
def forbid_live_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Draft inspection must not connect to a device or database")

    monkeypatch.setattr(cli, "AndroidDevice", forbidden)
    monkeypatch.setattr(cli.Repository, "connect", forbidden)


@pytest.fixture
def draft_path(tmp_path):
    path = tmp_path / "draft.toml"
    path.write_text(
        'name = "SYNTHETIC login-only draft"\n'
        'verified = false\nsynthetic = false\n'
        'app_package = "test.synthetic.app"\napp_version = "SYNTHETIC-1"\n'
        'reference_resolution = [100, 200]\n'
        '[pages.login]\nall = [{resource_id = "synthetic/login"}]\n'
        '[actions.search_entry]\nresource_id = "synthetic/button"\n'
    )
    return path


def saved_sample(tmp_path, *, metadata=None, duplicate_action=False):
    xml = (
        '<hierarchy synthetic="true"><node resource-id="synthetic/login"/>'
        '<node resource-id="synthetic/button" bounds="[10,20][80,60]"/>'
    )
    if duplicate_action:
        xml += '<node resource-id="synthetic/button" bounds="[10,80][80,120]"/>'
    xml += "</hierarchy>"
    properties = {
        "source_kind": "synthetic", "package": "test.synthetic.app",
        "configured_package": "test.synthetic.app", "app_version": "SYNTHETIC-1",
        "resolution": [100, 200],
        **(metadata or {}),
    }
    store = EvidenceStore(tmp_path / "evidence")
    manifest = store.save(
        Snapshot(xml=xml, png=b"SYNTHETIC screenshot fixture", metadata=properties),
        device_id="synthetic-device", run_id="synthetic-run", label="synthetic-login",
    )
    return store.root / manifest["evidence_id"]


def invoke_draft(path, sample=None):
    args = ["profile-check", "--draft", "--profile", str(path)]
    if sample is not None:
        args += ["--sample", str(sample)]
    return runner.invoke(cli.app, args)


def test_partial_draft_reports_matches_and_gaps_without_promotion(draft_path, tmp_path):
    before = draft_path.read_bytes()
    result = invoke_draft(draft_path, saved_sample(tmp_path))
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["mode"] == "draft"
    assert report["verified"] is False and report["production_validated"] is False
    assert report["page_rule_matches"]["login"] == {
        "all_counts": [1], "any_counts": [], "none_counts": [], "activity_match": None,
        "matched": True,
    }
    assert report["action_matches"]["search_entry"] == {
        "match_count": 1, "unique_bounds": [10, 20, 80, 60],
    }
    assert {"pages.home", "pages.search", "fields.body", "body_complete_marker"} <= set(
        report["missing_production_requirements"]
    )
    assert draft_path.read_bytes() == before
    assert load_profile(draft_path, require_verified=False).verified is False
    assert "eligible" not in report and "field_readability" not in report


def test_draft_without_sample_does_not_claim_metadata_was_checked(draft_path):
    result = invoke_draft(draft_path)
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["sample_checked"] is False
    assert all(check["status"] == "not_checked" for check in report["metadata_checks"].values())


def test_profile_check_preserves_crlf_evidence_bytes(draft_path, tmp_path, monkeypatch):
    from xhs_mobile import inspection

    raw_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        '<hierarchy synthetic="true">\r\n'
        '  <node resource-id="synthetic/login" text="SYNTHETIC 中文"/>\r\n'
        '</hierarchy>\r\n'
    ).encode()
    digest = hashlib.sha256(raw_xml).hexdigest()
    store = EvidenceStore(tmp_path / "synthetic-evidence")
    manifest = store.save(
        Snapshot(
            xml=raw_xml.decode("utf-8"), png=b"SYNTHETIC CRLF screenshot fixture",
            metadata={"source_kind": "synthetic", "synthetic_xml_sha256": digest},
        ),
        device_id="synthetic-device", run_id="synthetic-run", label="synthetic-crlf",
    )
    sample = store.root / manifest["evidence_id"]
    assert manifest["files"]["xml"]["sha256"] == digest
    received = []

    def inspect_exact_bytes(profile, snapshot):
        assert snapshot.xml.encode("utf-8") == raw_xml
        assert hashlib.sha256(snapshot.xml.encode("utf-8")).hexdigest() == digest
        received.append(snapshot)
        return {"ok": True, "synthetic": True, "mode": "draft"}

    monkeypatch.setattr(inspection, "inspect_draft", inspect_exact_bytes)
    result = invoke_draft(draft_path, sample)
    assert result.exit_code == 0, result.stdout
    assert len(received) == 1
    assert (sample / "ui.xml").read_bytes() == raw_xml
    assert store.verify(manifest)


def test_draft_page_exclusions_match_production_semantics(draft_path, tmp_path):
    draft_path.write_text(draft_path.read_text().replace(
        '[pages.login]\n',
        '[pages.login]\nnone = [{resource_id = "synthetic/button"}]\n',
    ))
    result = invoke_draft(draft_path, saved_sample(tmp_path))
    assert result.exit_code == 0, result.stdout
    page = json.loads(result.stdout)["page_rule_matches"]["login"]
    assert page == {"all_counts": [1], "any_counts": [], "none_counts": [1],
                    "activity_match": None, "matched": False}


@pytest.mark.parametrize("activity,expected", [
    ("test.synthetic.app.NoteDetailActivity", True),
    (None, False),
    ("NoteDetailActivity", False),
    ("test.synthetic.app.SearchImageDetailActivity", False),
])
def test_draft_activity_match_agrees_with_page_rules(draft_path, tmp_path, activity, expected):
    draft_path.write_text(draft_path.read_text().replace(
        '[pages.login]\n', '[pages.login]\nactivity = "test.synthetic.app.NoteDetailActivity"\n',
    ))
    result = invoke_draft(draft_path, saved_sample(tmp_path, metadata={"activity": activity}))
    assert result.exit_code == 0, result.stdout
    page = json.loads(result.stdout)["page_rule_matches"]["login"]
    assert page["activity_match"] is expected
    assert page["matched"] is expected


def test_draft_reports_only_configured_filter_menu_requirements(draft_path):
    result = invoke_draft(draft_path)
    missing = json.loads(result.stdout)["missing_production_requirements"]
    assert "pages.filter" not in missing and "image_text_selected_marker" not in missing
    with draft_path.open("a") as handle:
        handle.write('\n[actions.filter_entry]\ntext = "SYNTHETIC filters"\n')
    result = invoke_draft(draft_path)
    assert result.exit_code == 0, result.stdout
    missing = json.loads(result.stdout)["missing_production_requirements"]
    assert {"pages.filter", "actions.image_text_filter", "actions.filter_confirm",
            "image_text_selected_marker"} <= set(missing)


def test_missing_draft_metadata_is_explicit_and_no_action_bounds_are_assumed(draft_path, tmp_path):
    content = draft_path.read_text().replace(
        'app_package = "test.synthetic.app"', 'app_package = ""'
    )
    content = content.replace('app_version = "SYNTHETIC-1"', 'app_version = ""')
    content = content.replace("reference_resolution = [100, 200]", "reference_resolution = [0, 0]")
    draft_path.write_text(content)
    result = invoke_draft(draft_path, saved_sample(tmp_path))
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert all(
        check == {"status": "not_checked", "reason": "not_configured"}
        for check in report["metadata_checks"].values()
    )
    assert report["action_matches"]["search_entry"] == {"match_count": 1, "unique_bounds": None}


@pytest.mark.parametrize("component", ["ui.xml", "screen.png"])
def test_corrupted_saved_evidence_is_rejected(draft_path, tmp_path, component):
    sample = saved_sample(tmp_path)
    (sample / component).write_bytes(b"SYNTHETIC altered evidence")
    result = invoke_draft(draft_path, sample)
    assert result.exit_code == 2
    assert "EvidenceError" in result.stdout


@pytest.mark.parametrize("metadata", [
    {"package": "another.synthetic.app"},
    {"configured_package": "another.synthetic.app"},
    {"app_version": "SYNTHETIC-2"},
    {"resolution": [200, 100]},
    {"package": None},
    {"app_version": None},
    {"resolution": None},
])
def test_explicit_metadata_mismatch_or_unavailable_sample_metadata_fails(
    draft_path, tmp_path, metadata,
):
    result = invoke_draft(draft_path, saved_sample(tmp_path, metadata=metadata))
    assert result.exit_code == 2
    assert "ProfileError" in result.stdout


def test_synthetic_sample_is_labeled_even_when_profile_flag_is_false(draft_path, tmp_path):
    result = invoke_draft(draft_path, saved_sample(tmp_path))
    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["profile_synthetic"] is False
    assert report["sample_synthetic"] is True and report["synthetic"] is True


def test_ambiguous_action_does_not_choose_a_click_target(draft_path, tmp_path):
    result = invoke_draft(draft_path, saved_sample(tmp_path, duplicate_action=True))
    assert result.exit_code == 0
    assert json.loads(result.stdout)["action_matches"]["search_entry"] == {
        "match_count": 2, "unique_bounds": None,
    }


@pytest.mark.parametrize("command", ["profile-check", "run", "collect-current"])
def test_draft_is_still_rejected_by_strict_commands(draft_path, tmp_path, command):
    config = tmp_path / "config.toml"
    config.write_text(
        f"profile_path = {json.dumps(str(draft_path))}\n"
        '[devices.lab01]\nserial_env = "SYNTHETIC_UNUSED_SERIAL"\n'
        'session_ref = "synthetic-session"\napp_package = "test.synthetic.app"\n'
    )
    args = ["--config", str(config), command]
    if command == "profile-check":
        args += ["--profile", str(draft_path)]
    else:
        args += ["--device", "lab01"]
        if command == "run":
            args += ["--keyword", "SYNTHETIC keyword"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "verified, non-synthetic" in result.stdout


def test_draft_mode_still_rejects_invalid_rules(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text("verified = false\n[pages.login]\nall = [{}]\n")
    result = invoke_draft(path)
    assert result.exit_code == 2
    assert "selector" in result.stdout


def complete_strict_sample(tmp_path):
    # These are still SYNTHETIC fixtures. The false profile flag below exercises
    # the strict CLI branch and does not make the fixtures real-device evidence.
    fixtures = Path(__file__).parent / "fixtures"
    content = (fixtures / "synthetic_profile.toml").read_text()
    content = content.replace("synthetic = true", "synthetic = false")
    content = content.replace(
        '"SYNTHETIC: fixtures created for offline testing, not actual UI"',
        json.dumps(str(fixtures / "synthetic_detail.xml")),
    )
    profile_path = tmp_path / "complete-test-profile.toml"
    profile_path.write_text(content)
    store = EvidenceStore(tmp_path / "evidence")
    manifest = store.save(
        Snapshot(
            xml=(fixtures / "synthetic_detail.xml").read_text(),
            png=b"SYNTHETIC strict CLI sample",
            metadata={
                "source_kind": "synthetic", "package": "test.synthetic.app",
                "app_version": "SYNTHETIC-1", "resolution": [1080, 1920],
            },
        ),
        device_id="synthetic-device", run_id="synthetic-run", label="synthetic-detail",
    )
    return profile_path, store.root / manifest["evidence_id"]


def test_complete_strict_profile_check_keeps_existing_sample_parsing(tmp_path):
    profile_path, sample = complete_strict_sample(tmp_path)
    result = runner.invoke(cli.app, [
        "profile-check", "--profile", str(profile_path),
        "--sample", str(sample),
    ])
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["page"] == "detail"
    assert "body" in report["field_readability"]
    assert "mode" not in report and report["verified"] is True


@pytest.mark.parametrize("draft", [True, False])
def test_copied_manifest_cannot_validate_another_sample_directory(
    draft_path, tmp_path, draft,
):
    if draft:
        profile_path, verified_sample = draft_path, saved_sample(tmp_path)
    else:
        profile_path, verified_sample = complete_strict_sample(tmp_path)
    copied_manifest = (verified_sample / "manifest.json").read_bytes()
    assert EvidenceStore(verified_sample.parent).verify(json.loads(copied_manifest))
    other_id = "0" * 32 if verified_sample.name != "0" * 32 else "1" * 32
    unverified_sample = verified_sample.parent / other_id
    unverified_sample.mkdir()
    (unverified_sample / "manifest.json").write_bytes(copied_manifest)
    # The XML remains parseable and matches the configured page. Only its bytes
    # and screenshot provenance differ, so metadata/page checks cannot catch this.
    unverified_xml = (verified_sample / "ui.xml").read_text().replace(
        "</hierarchy>", '<node resource-id="synthetic/unverified-extra"/></hierarchy>',
    )
    (unverified_sample / "ui.xml").write_text(unverified_xml)
    (unverified_sample / "screen.png").write_bytes(b"SYNTHETIC unverified sibling screenshot")
    args = ["profile-check", "--profile", str(profile_path), "--sample", str(unverified_sample)]
    if draft:
        args.append("--draft")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    report = json.loads(result.stdout)
    assert report["error"] == "EvidenceError"
    assert "directory" in report["message"].lower()
