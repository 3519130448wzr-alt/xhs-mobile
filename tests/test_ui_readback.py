"""SYNTHETIC node evidence only; no device connections or genuine observations."""

import copy
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from xhs_mobile.domain import Snapshot
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.ui_readback import capture_hierarchy, resolve_ui_text, xml_sanitized

PACKAGE = "org.synthetic.notes"
RESOURCE = "org.synthetic.notes:id/text"


def node(text="陈尔摩斯..", **attrs):
    return ET.Element("node", {
        "package": PACKAGE, "class": "android.widget.TextView", "resource-id": RESOURCE,
        "bounds": "[20,40][500,120]", "text": text, "content-desc": "",
        "visible-to-user": "true", "password": "false", **attrs,
    })


def xml(*nodes):
    root = ET.Element("hierarchy", {"synthetic": "true"})
    root.extend(nodes or [node()])
    return ET.tostring(root, encoding="unicode")


def info(text="陈尔摩斯🥕", **values):
    return {
        "packageName": PACKAGE, "className": "android.widget.TextView",
        "resourceName": RESOURCE, "text": text, "contentDescription": "",
        "visibleBounds": {"left": 20, "top": 40, "right": 500, "bottom": 120},
        **values,
    }


class FakeDevice:
    def __init__(self, before=None, *, after=None, objects=None, verification=None):
        self.before = xml() if before is None else before
        self.after = self.before if after is None else after
        self.objects = [info()] if objects is None else objects
        self.verification = info() if verification is None else verification
        self.dumps = 0
        self.calls = []
        self.info = {"displayWidth": 720, "displayHeight": 1280}

    def dump_hierarchy(self, *, compressed):
        assert compressed is False
        self.dumps += 1
        return self.before if self.dumps == 1 else self.after

    def __call__(self, **selector):
        self.calls.append(selector)
        outer = self

        class Object:
            def info_list(self):
                if isinstance(outer.objects, Exception):
                    raise outer.objects
                return copy.deepcopy(outer.objects)

            @property
            def info(self):
                return copy.deepcopy(outer.verification)

        return Object()


def capture(device=None):
    device = device or FakeDevice()
    result = capture_hierarchy(device, PACKAGE)
    png = b"SYNTHETIC screenshot bytes"
    result["readbacks"]["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
    shot = Snapshot(result["xml"], png, metadata={
        "source_kind": "synthetic", "configured_package": PACKAGE, "package": PACKAGE,
        "resolution": [720, 1280], "ui_readbacks": result["readbacks"],
    })
    return shot, ET.fromstring(shot.xml)


@pytest.mark.parametrize(("original", "expected"), [
    ("陈尔摩斯🥕", "陈尔摩斯.."), ("🉑", ".."), ("❗️", "❗️"),
    ("𠀀", ".."), ("literal..", "literal.."), ("a\x01b", "a.b"),
])
def test_sanitizer_is_comparison_only_and_matches_utf16_behavior(original, expected):
    assert xml_sanitized(original) == expected


def test_preserves_exact_supplementary_text_and_description_without_modifying_xml():
    before = xml(node(**{"content-desc": "可..测试"}))
    device = FakeDevice(before, objects=[info(contentDescription="可🉑测试")])
    shot, root = capture(device)
    assert shot.xml == before
    assert root[0].get("text") == "陈尔摩斯.."
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯🥕", "ui_objinfo")
    assert resolve_ui_text(root, root[0], shot, "content-desc") == ("可🉑测试", "ui_objinfo")
    assert device.dumps == 2
    assert device.calls == [{
        "packageName": PACKAGE, "resourceId": RESOURCE, "className": "android.widget.TextView",
    }]


def test_legacy_list_info_requires_second_raw_text_identity_verification():
    device = FakeDevice(objects=[info(resourceName=None)])
    shot, root = capture(device)
    assert device.calls[-1]["text"] == "陈尔摩斯🥕"
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯🥕", "ui_objinfo")
    shot.metadata["ui_readbacks"]["entries"][0]["verification_selector"]["text"] = "guessed"
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


def test_legacy_list_info_rejects_mismatching_resource_verification():
    shot, root = capture(FakeDevice(
        objects=[info(resourceName=None)], verification=info(resourceName="different-resource")
    ))
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


@pytest.mark.parametrize(("legacy_description", "modern_description", "method"), [
    ("", None, "ui_objinfo"), (None, "", "ui_objinfo"),
    ("nonempty", None, "ui"), ("", "changed", "ui"), (False, None, "ui"),
])
def test_legacy_empty_description_and_modern_null_are_the_same_absent_value(
    legacy_description, modern_description, method
):
    device = FakeDevice(
        objects=[info(resourceName=None, contentDescription=legacy_description)],
        verification=info(contentDescription=modern_description),
    )
    shot, root = capture(device)
    assert resolve_ui_text(root, root[0], shot)[1] == method
    group = shot.metadata["ui_readbacks"]["groups"][0]
    entry = shot.metadata["ui_readbacks"]["entries"][0]
    assert group["objects"][0]["contentDescription"] == legacy_description
    assert entry["raw_objinfo"]["contentDescription"] == modern_description


@pytest.mark.parametrize("text", ["陈尔摩斯..", "说点什么...", "arbitrary literal..text"])
@pytest.mark.parametrize("resource_name", [RESOURCE, None])
def test_literal_dots_skip_identity_rpc_and_second_dump_without_guessing(text, resource_name):
    device = FakeDevice(
        xml(node(text)), after=xml(node("SYNTHETIC later page")),
        objects=[info(text=text, resourceName=resource_name)],
    )
    shot, root = capture(device)
    assert resolve_ui_text(root, root[0], shot) == (text, "ui")
    metadata = shot.metadata["ui_readbacks"]
    assert metadata["entries"] == []
    assert metadata["status"] == "unavailable"
    assert metadata["reasons"] == ["no_unique_lossless_mapping"]
    assert metadata["stability_check"] == "not_performed_no_supplement"
    assert metadata["result_count"] == 1
    assert metadata["xml_sha256"] == hashlib.sha256(device.before.encode("utf-8")).hexdigest()
    assert metadata["screenshot_sha256"] == hashlib.sha256(shot.png).hexdigest()
    assert shot.xml == device.before
    assert device.dumps == 1
    assert device.calls == [{
        "packageName": PACKAGE, "resourceId": RESOURCE, "className": "android.widget.TextView",
    }]


def test_changed_description_still_gets_identity_and_stability_checks():
    before = xml(node("SYNTHETIC label", **{"content-desc": "描述.."}))
    device = FakeDevice(
        before, objects=[info("SYNTHETIC label", resourceName=None, contentDescription="描述🉑")],
        verification=info("SYNTHETIC label", contentDescription="描述🉑"),
    )
    shot, root = capture(device)
    assert device.dumps == 2
    assert len(device.calls) == 2
    assert device.calls[1]["text"] == "SYNTHETIC label"
    assert resolve_ui_text(root, root[0], shot) == ("SYNTHETIC label", "ui")
    assert resolve_ui_text(root, root[0], shot, "content-desc") == ("描述🉑", "ui_objinfo")


def test_no_unique_candidates_keep_original_tree_without_claiming_page_stability():
    device = FakeDevice(objects=[info(), info()], after=xml(node("SYNTHETIC later page")))
    shot, root = capture(device)
    assert shot.xml == device.before
    assert device.dumps == 1
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")
    assert shot.metadata["ui_readbacks"]["status"] == "unavailable"
    assert shot.metadata["ui_readbacks"]["stability_check"] == "not_performed_no_supplement"


@pytest.mark.parametrize("objects", [
    [info(), info()], [info(packageName="org.synthetic.wrong")],
    [info(className="android.widget.Button")], [info(resourceName="wrong-resource")],
    [info(text="猜测🥕")],
    [info(visibleBounds={"left": 21, "top": 40, "right": 500, "bottom": 120})],
    [info(visibleBounds={"left": True, "top": 40, "right": 500, "bottom": 120})],
])
def test_ambiguous_or_mismatching_objinfo_is_not_accepted(objects):
    shot, root = capture(FakeDevice(objects=objects))
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


def test_one_object_cannot_map_to_two_xml_nodes():
    shot, root = capture(FakeDevice(xml(node(), node(index="1"))))
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")
    assert resolve_ui_text(root, root[1], shot) == ("陈尔摩斯..", "ui")


@pytest.mark.parametrize("bounds", ["[-1,40][500,120]", "[20,40][721,120]", "[20,40][500,1281]"])
def test_out_of_screen_xml_bounds_are_not_supplemented(bounds):
    shot, root = capture(FakeDevice(xml(node(bounds=bounds))))
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


@pytest.mark.parametrize("after", [
    xml(node(index="1")), xml(node(), node("new candidate", bounds="[20,200][500,240]")),
    xml(node("new candidate", bounds="[20,200][500,240]"), node()),
])
def test_changed_target_or_candidate_set_keeps_new_xml_without_supplement(after):
    shot, root = capture(FakeDevice(after=after))
    assert shot.xml == after
    assert shot.metadata["ui_readbacks"]["reasons"] == ["page_changed"]
    assert shot.metadata["ui_readbacks"]["entries"] == []


@pytest.mark.parametrize("before", [
    xml(*[node(index=str(i)) for i in range(33)]),
    xml(*[node(**{"resource-id": f"synthetic-resource-{i}"}) for i in range(9)]),
])
def test_target_and_group_budgets_prevent_objinfo_requests(before):
    device = FakeDevice(before)
    shot, _ = capture(device)
    assert shot.xml == before
    assert shot.metadata["ui_readbacks"]["reasons"] == ["readback_budget_exceeded"]
    assert device.calls == []


def test_result_budget_returns_original_xml_without_publishing_partial_readbacks():
    device = FakeDevice(objects=[info()] * 129)
    shot, _ = capture(device)
    assert shot.xml == device.before
    assert shot.metadata["ui_readbacks"]["reasons"] == ["readback_budget_exceeded"]
    assert shot.metadata["ui_readbacks"]["entries"] == []


def test_rpc_failure_retains_original_xml_and_an_unavailable_reason():
    device = FakeDevice(objects=RuntimeError("SYNTHETIC raw content must not appear in errors"))
    shot, _ = capture(device)
    assert shot.xml == device.before
    assert shot.metadata["ui_readbacks"]["reasons"] == ["objinfo_unavailable_RuntimeError"]


def test_no_loss_candidates_do_not_trigger_objinfo():
    device = FakeDevice(xml(node("完整❗️")))
    shot, root = capture(device)
    assert resolve_ui_text(root, root[0], shot) == ("完整❗️", "ui")
    assert device.calls == []
    assert device.dumps == 1


@pytest.mark.parametrize("change", [
    lambda data: data.update(xml_sha256="bad"),
    lambda data: data.update(screenshot_sha256="bad"),
    lambda data: data.update(configured_package="other"),
    lambda data: data.update(resolution=[5000, 5000]),
    lambda data: data.update(result_count=129),
    lambda data: data["entries"].append(copy.deepcopy(data["entries"][0])),
    lambda data: data["entries"][0].update(xml_value="not the original"),
    lambda data: data["entries"][0].update(raw_value="guessed🉑"),
    lambda data: data["entries"][0].update(node_path=[-1]),
    lambda data: data["entries"][0]["raw_objinfo"].update(packageName="other"),
    lambda data: data["entries"][0]["raw_objinfo"].update(className="other"),
    lambda data: data["entries"][0]["raw_objinfo"].update(resourceName="other"),
    lambda data: data["groups"][0]["objects"].append(
        copy.deepcopy(data["groups"][0]["objects"][0])
    ),
    lambda data: data["groups"][0].update(before_nodes=[]),
    lambda data: data["groups"][0].update(after_nodes=[]),
])
def test_resolver_rechecks_evidence_instead_of_trusting_status(change):
    shot, root = capture()
    change(shot.metadata["ui_readbacks"])
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


def test_resolver_rejects_a_node_from_another_tree_or_modified_attributes():
    shot, root = capture()
    unrelated = node()
    assert resolve_ui_text(root, unrelated, shot) == ("陈尔摩斯..", "ui")
    root[0].set("clickable", "true")
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯..", "ui")


@pytest.mark.parametrize("before", [
    xml(node(package="org.synthetic.other")), xml(node(password="true")),
    '<hierarchy synthetic="true"><node visible-to-user="false">'
    + ET.tostring(node(), encoding="unicode") + '</node></hierarchy>',
])
def test_other_packages_hidden_nodes_and_passwords_do_not_trigger_reads(before):
    device = FakeDevice(before)
    shot, _ = capture(device)
    assert device.calls == []
    assert shot.metadata["ui_readbacks"]["entries"] == []


def test_lossless_metadata_is_saved_with_verified_original_evidence(tmp_path):
    shot, root = capture()
    store = EvidenceStore(tmp_path / "synthetic-evidence")
    manifest = store.save(
        shot, device_id="SYNTHETIC-device", run_id="SYNTHETIC-run", label="synthetic-readback"
    )
    assert store.verify(manifest)
    saved = json.loads((store.root / manifest["manifest_path"]).read_text())
    assert saved["metadata"]["ui_readbacks"]["entries"][0]["raw_value"] == "陈尔摩斯🥕"
    assert (store.root / manifest["files"]["xml"]["path"]).read_text() == shot.xml
    assert resolve_ui_text(root, root[0], shot) == ("陈尔摩斯🥕", "ui_objinfo")


def test_json_roundtrip_with_crlf_xml_preserves_valid_readback_and_rejects_normalized_xml(tmp_path):
    original = '<?xml version="1.0" encoding="UTF-8"?>\r\n' + xml().replace('><', '>\r\n<')
    shot, _ = capture(FakeDevice(
        original, objects=[info(resourceName=None, contentDescription="")],
        verification=info(contentDescription=None),
    ))
    store = EvidenceStore(tmp_path / "synthetic-evidence")
    manifest = store.save(
        shot, device_id="SYNTHETIC-device", run_id="SYNTHETIC-run", label="synthetic-crlf"
    )
    saved = json.loads((store.root / manifest["manifest_path"]).read_text())
    assert store.verify(saved)
    xml_path = store.root / saved["files"]["xml"]["path"]
    loaded = Snapshot(
        xml=xml_path.read_bytes().decode("utf-8"),
        png=(store.root / saved["files"]["png"]["path"]).read_bytes(),
        metadata=saved["metadata"],
    )
    assert loaded.xml == original
    root = ET.fromstring(loaded.xml)
    assert resolve_ui_text(root, root[0], loaded) == ("陈尔摩斯🥕", "ui_objinfo")
    loaded.xml = xml_path.read_text()
    assert loaded.xml != original
    root = ET.fromstring(loaded.xml)
    assert resolve_ui_text(root, root[0], loaded) == ("陈尔摩斯..", "ui")
