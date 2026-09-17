from pathlib import Path

import pytest
from pydantic import ValidationError

from xhs_mobile.domain import ProfileError, Snapshot
from xhs_mobile.profile import (
    FieldRule,
    PageRule,
    Profile,
    Selector,
    classify,
    load_profile,
    locate,
    matching,
    validate_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return load_profile(FIXTURES / "synthetic_profile.toml", require_verified=False)


def snap(nodes: str = "", **metadata):
    return Snapshot(xml=f"<hierarchy>{nodes}</hierarchy>", png=b"",
                    metadata={"app_version": "SYNTHETIC-1", "resolution": [1080, 1920],
                              **metadata})


def test_production_loader_rejects_template_and_synthetic():
    with pytest.raises(ProfileError, match="verified, non-synthetic"):
        load_profile("profiles/template.toml")
    with pytest.raises(ProfileError, match="verified, non-synthetic"):
        load_profile(FIXTURES / "synthetic_profile.toml")
    assert not load_profile("profiles/template.toml", require_verified=False).verified


def test_profile_rejects_empty_broad_rules_and_missing_metadata():
    with pytest.raises(ValidationError):
        Selector()
    with pytest.raises(ValidationError):
        PageRule()
    with pytest.raises(ValidationError):
        Profile(verified=True)
    with pytest.raises(ValidationError):
        Profile(pages={"arbitrary": PageRule(all=[Selector(text="a")])})


def test_verified_loader_requires_readable_nonempty_evidence(tmp_path):
    # SYNTHETIC loader unit test, no device/collection. This verifies file checking,
    # not the truth of a human's calibration claim.
    text = (FIXTURES / "synthetic_profile.toml").read_text()
    text = text.replace("synthetic = true", "synthetic = false")
    text = text.replace(
        '"SYNTHETIC: fixtures created for offline testing, not actual UI"', '"evidence.xml"'
    )
    profile_path = tmp_path / "test_profile.toml"
    profile_path.write_text(text)
    with pytest.raises(ProfileError, match="Cannot read calibration evidence"):
        load_profile(profile_path)
    evidence = tmp_path / "evidence.xml"
    evidence.write_text("")
    with pytest.raises(ProfileError, match="evidence is empty"):
        load_profile(profile_path)
    evidence.write_text("<SYNTHETIC-not-real-evidence/>")
    assert load_profile(profile_path).verified


def test_snapshot_drift_is_explicit(profile):
    validate_snapshot(snap(), profile)
    # Bootstrap diagnostic snapshots may have no configured package while still
    # positively reporting the actual foreground package and its version.
    validate_snapshot(snap(configured_package="", package=profile.app_package), profile)
    for metadata in ({"app_version": "new"}, {"resolution": [1920, 1080]},
                     {"app_package": "wrong.app"}, {"resolution": None},
                     {"configured_package": "wrong.app"}, {"package": "other.app"},
                     {"package": None}):
        with pytest.raises(ProfileError):
            classify(snap(**metadata), profile)


def test_classification_positive_markers_alerts_and_conflicts(profile):
    detail = '<node resource-id="synthetic/image-text-detail"/>'
    rate = '<node resource-id="synthetic/rate-limited"/>'
    assert classify(snap(detail), profile) == "detail"
    assert classify(snap(), profile) == "unknown"
    assert classify(snap(detail + rate), profile) == "rate_limited"
    assert classify(snap(detail + '<node resource-id="synthetic/video"/>'), profile) == "unknown"
    assert classify(snap(rate + '<node resource-id="synthetic/login"/>'), profile) == "unknown"


def test_hidden_ancestors_do_not_match(profile):
    xml = ('<node visible-to-user="false">'
           '<node resource-id="synthetic/image-text-detail"/></node>')
    assert classify(snap(xml), profile) == "unknown"


def test_all_any_exact_match(profile):
    profile.pages = {"home": PageRule(all=[Selector(text="A")],
                                      any=[Selector(content_desc="B"), Selector(text="C")])}
    assert classify(snap('<node text="A"/><node text="C"/>'), profile) == "home"
    assert classify(snap('<node text="AA"/><node text="C"/>'), profile) == "unknown"
    assert classify(snap('<node text="A"/>'), profile) == "unknown"


def test_action_bounds_and_ambiguity(profile):
    node = '<node resource-id="synthetic/search-entry" bounds="[0,5][100,50]"/>'
    assert locate(snap(node), profile, "search_entry") == (0, 5, 100, 50)
    assert locate(snap(node + node), profile, "search_entry") is None
    assert locate(snap(node.replace("100,50", "2000,50")), profile, "search_entry") is None
    assert locate(snap(node), profile, "unknown_action") is None


def test_xml_dtd_and_invalid_xml_rejected(profile):
    for xml in ('<!DOCTYPE x [<!ENTITY y "oops">]><x>&y;</x>', '<node'):
        snapshot = snap()
        snapshot.xml = xml
        with pytest.raises(ProfileError):
            classify(snapshot, profile)


def test_invalid_ocr_regions():
    with pytest.raises(ValidationError):
        FieldRule(ocr_region=(10, 10, 5, 20))
    with pytest.raises(ValidationError):
        Profile(reference_resolution=(100, 200),
                fields={"body": FieldRule(ocr_region=(0, 0, 200, 200))})


def test_exact_state_attributes_and_package_isolate_app_nodes(profile):
    profile.pages = {"search": PageRule(all=[Selector(
        class_name="android.widget.EditText", package_name="org.synthetic.notes",
        clickable=True, focused=True, selected=False,
    )])}
    node = ('<node class="android.widget.EditText" package="org.synthetic.notes" '
            'clickable="true" focused="true" selected="false"/>')
    assert classify(snap(node), profile) == "search"
    for altered in (
        node.replace('package="org.synthetic.notes"', 'package="org.synthetic.keyboard"'),
        node.replace('focused="true"', 'focused="false"'),
        node.replace('selected="false"', 'selected="true"'),
        node.replace('clickable="true"', ''),
    ):
        assert classify(snap(altered), profile) == "unknown"


def test_parent_constraints_are_immediate_scoped_and_hide_ancestors():
    import xml.etree.ElementTree as ET

    selector = Selector(class_name="image", parent=Selector(
        class_name="body", parent=Selector(class_name="card", clickable=True),
    ))
    direct = ('<node class="card" clickable="true"><node class="body">'
              '<node class="image"/></node></node>')
    root = ET.fromstring(f"<hierarchy>{direct}</hierarchy>")
    assert len(matching(root, selector)) == 1
    # The same classes somewhere above a node do not satisfy immediate parent paths.
    nested = direct.replace(
        '<node class="image"/>', '<node class="extra"><node class="image"/></node>'
    )
    assert not matching(ET.fromstring(f"<hierarchy>{nested}</hierarchy>"), selector)
    hidden = direct.replace('class="card"', 'class="card" visible-to-user="false"')
    assert not matching(ET.fromstring(f"<hierarchy>{hidden}</hierarchy>"), selector)
    assert not matching(root.find('.//node[@class="image"]'), selector)
    with pytest.raises(ValidationError):
        Selector(parent=Selector(class_name="card"))


def test_page_exclusions_prevent_acting_through_overlay(profile):
    profile.pages = {"results": PageRule(
        all=[Selector(text="SYNTHETIC results")], none=[Selector(text="SYNTHETIC overlay")],
    )}
    base = '<node text="SYNTHETIC results"/>'
    assert classify(snap(base), profile) == "results"
    assert classify(snap(base + '<node text="SYNTHETIC overlay"/>'), profile) == "unknown"
    hidden = '<node visible-to-user="false"><node text="SYNTHETIC overlay"/></node>'
    assert classify(snap(base + hidden), profile) == "results"
    with pytest.raises(ValidationError):
        PageRule(none=[Selector(text="SYNTHETIC overlay")])


def test_content_description_prefix_is_literal_and_composes_with_parent():
    import xml.etree.ElementTree as ET

    selector = Selector(class_name="text", parent=Selector(
        class_name="button", content_desc_prefix="SYNTHETIC like ",
    ))
    def source(description):
        return ET.fromstring(
            f'<hierarchy><node class="button" content-desc="{description}">'
            '<node class="text" text="1.2万"/></node></hierarchy>'
        )
    assert len(matching(source("SYNTHETIC like 1.2万"), selector)) == 1
    assert not matching(source("other SYNTHETIC like 1.2万"), selector)
    assert not matching(source("SYNTHETIC likes 1.2万"), selector)
    literal = Selector(content_desc_prefix="SYNTHETIC .* ")
    assert not matching(source("SYNTHETIC like 1.2万"), literal)
    for prefix in ("", "   "):
        with pytest.raises(ValidationError):
            Selector(class_name="button", content_desc_prefix=prefix)


def test_xml_index_matches_reported_attribute_not_child_position():
    import xml.etree.ElementTree as ET

    root = ET.fromstring('<hierarchy><node class="text" index="0"/>'
                         '<node class="text" index="2"/><node class="text"/></hierarchy>')
    assert matching(root, Selector(class_name="text", xml_index=2)) == [root[1]]
    assert not matching(root, Selector(class_name="text", xml_index=1))
    for value in (-1, 1.5, "2", True):
        with pytest.raises(ValidationError):
            Selector(class_name="text", xml_index=value)
    with pytest.raises(ValidationError):
        Selector(xml_index=0)


@pytest.mark.parametrize("region", [(0, 10, 0, 20), (0, -1, 10, 20), (0, 0, 101, 200)])
def test_invalid_body_visible_region(region):
    with pytest.raises(ValidationError):
        Profile(reference_resolution=(100, 200), body_visible_region=region)


@pytest.mark.parametrize("budget", [-1, 6])
def test_detail_body_swipe_budget_is_bounded(budget):
    with pytest.raises(ValidationError):
        Profile(detail_body_swipes=budget)


@pytest.mark.parametrize("activity,expected", [
    ("org.synthetic.notes.NoteDetailActivity", "detail"),
    (None, "unknown"),
    ("NoteDetailActivity", "unknown"),
    ("org.synthetic.notes.SearchImageDetailActivity", "unknown"),
    ("other.org.synthetic.notes.NoteDetailActivity", "unknown"),
])
def test_page_activity_is_exact_and_requires_metadata(profile, activity, expected):
    profile.pages = {"detail": PageRule(
        activity="org.synthetic.notes.NoteDetailActivity",
        all=[Selector(text="SYNTHETIC detail")],
    )}
    assert classify(snap('<node text="SYNTHETIC detail"/>', activity=activity), profile) == expected
    assert classify(snap(activity="org.synthetic.notes.NoteDetailActivity"), profile) == "unknown"


def test_activity_cannot_replace_positive_marker_or_be_empty():
    with pytest.raises(ValidationError):
        PageRule(activity="org.synthetic.notes.NoteDetailActivity")
    for activity in ("", "   "):
        with pytest.raises(ValidationError):
            PageRule(activity=activity, all=[Selector(text="SYNTHETIC detail")])


@pytest.mark.parametrize("missing", ["page", "image_text_filter", "filter_confirm", "selected"])
def test_filter_menu_requires_complete_navigation_when_verified(profile, missing):
    profile.actions["filter_entry"] = Selector(text="SYNTHETIC filters")
    profile.actions["filter_confirm"] = Selector(text="SYNTHETIC confirm")
    profile.pages["filter"] = PageRule(all=[Selector(text="SYNTHETIC filter menu")])
    profile.image_text_selected_marker = Selector(text="SYNTHETIC image text", selected=True)
    data = profile.model_dump()
    assert Profile.model_validate(data).verified
    if missing == "page":
        del data["pages"]["filter"]
    elif missing == "selected":
        data["image_text_selected_marker"] = None
    else:
        del data["actions"][missing]
    with pytest.raises(ValidationError, match="filter_entry"):
        Profile.model_validate(data)
    data["verified"] = False
    assert not Profile.model_validate(data).verified


def test_legacy_single_filter_action_does_not_require_menu(profile):
    assert "image_text_filter" in profile.actions
    assert "filter_entry" not in profile.actions
    assert Profile.model_validate(profile.model_dump()).verified


@pytest.mark.parametrize("region", [(0, 10, 0, 20), (0, -1, 10, 20), (0, 0, 101, 200)])
def test_invalid_candidate_tap_region(region):
    with pytest.raises(ValidationError):
        Profile(reference_resolution=(100, 200), candidate_tap_region=region)


@pytest.mark.parametrize("height", [0, -1, 201])
def test_invalid_candidate_min_height(height):
    with pytest.raises(ValidationError):
        Profile(reference_resolution=(100, 200), candidate_min_height=height)


def test_rejected_suffixes_are_nonempty_literal_strings_without_stripping():
    with pytest.raises(ValidationError):
        FieldRule(selector=Selector(text="SYNTHETIC"), reject_text_suffixes=[""])
    rule = FieldRule(selector=Selector(text="SYNTHETIC"), reject_text_suffixes=["..", " "])
    assert rule.reject_text_suffixes == ["..", " "]
    with pytest.raises(ValidationError):
        FieldRule(selector=Selector(text="SYNTHETIC"), reject_text_contains=[""])
    rule = FieldRule(selector=Selector(text="SYNTHETIC"), reject_text_contains=["..", " "])
    assert rule.reject_text_contains == ["..", " "]
