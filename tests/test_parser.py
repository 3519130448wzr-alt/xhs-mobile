from pathlib import Path

import pytest

from xhs_mobile import parser
from xhs_mobile.domain import FieldValue, Snapshot
from xhs_mobile.parser import candidates, parse_count, parse_note
from xhs_mobile.profile import FieldRule, Selector, load_profile

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return load_profile(FIXTURES / "synthetic_profile.toml", require_verified=False)


@pytest.fixture
def snapshot():
    return Snapshot(xml=(FIXTURES / "synthetic_detail.xml").read_text(), png=b"",
                    metadata={"app_version": "SYNTHETIC-1", "resolution": [1080, 1920]})


@pytest.mark.parametrize("raw,value,approximate", [
    ("1.2万", 12000, True), ("1万+", 10000, True), ("0", 0, False),
    ("1,234", 1234, False), ("3.2k", 3200, True), ("99+", 99, True),
    ("1亿", 100000000, True),
])
def test_count_preserves_raw(raw, value, approximate):
    result = parse_count(raw)
    assert result.raw == raw
    assert result.normalized == value
    assert result.approximate is approximate


@pytest.mark.parametrize("raw", [None, "", "点赞", "--", "-1", "1.2", "1,23", "未知"])
def test_unread_counts_are_not_zero(raw):
    result = parse_count(raw)
    assert result.raw == raw
    assert result.normalized is None
    assert result.reason


def test_synthetic_note_quality_and_identity(snapshot, profile):
    note = parse_note(snapshot, profile)
    assert note.eligible
    assert note.note_id == "0123456789abcdef01234567"
    assert note.identity_source == "calibrated_ui_note_id"
    assert note.fields["likes"].approximate
    assert note.fields["favorites"].normalized == 0
    assert note.fields["comments"].normalized is None
    assert note.fields["tags"].raw == "#synthetic\n#测试"


@pytest.mark.parametrize("change", ["no_complete", "expand", "truncated", "missing_body"])
def test_completeness_markers_do_not_gate_readable_base_fields(snapshot, profile, change):
    if change == "no_complete":
        snapshot.xml = snapshot.xml.replace("synthetic/body-end", "synthetic/unrelated")
    elif change == "missing_body":
        snapshot.xml = snapshot.xml.replace('resource-id="synthetic/body"', 'resource-id="absent"')
    else:
        marker = "expand-body" if change == "expand" else "truncated"
        snapshot.xml = snapshot.xml.replace("</hierarchy>",
                                            f'<node resource-id="synthetic/{marker}"/></hierarchy>')
    assert not parse_note(snapshot, profile).body_complete
    assert parse_note(snapshot, profile).eligible is (change != "missing_body")


def test_title_missing_is_different_from_not_displayed(snapshot, profile):
    snapshot.xml = snapshot.xml.replace("synthetic/title", "synthetic/unknown-title")
    assert parse_note(snapshot, profile).fields["title"].status == "not_readable"
    assert not parse_note(snapshot, profile).eligible
    snapshot.xml = snapshot.xml.replace("</hierarchy>",
                                        '<node resource-id="synthetic/no-title"/></hierarchy>')
    assert parse_note(snapshot, profile).fields["title"].status == "not_displayed"
    assert parse_note(snapshot, profile).eligible


def test_not_positive_detail_is_never_eligible(snapshot, profile):
    snapshot.xml = snapshot.xml.replace("synthetic/image-text-detail", "synthetic/video")
    assert not parse_note(snapshot, profile).eligible
    assert parse_note(snapshot, profile).content_type == "video"


@pytest.mark.parametrize("url,expected", [
    ("https://www.xiaohongshu.com/explore/abcdef0123456789?token=secret", "abcdef0123456789"),
    ("https://xiaohongshu.com/discovery/item/abcdef0123456789?x=1", "abcdef0123456789"),
    ("https://xiaohongshu.com.evil.test/explore/abcdef0123456789", None),
    ("https://attacker@www.xiaohongshu.com/explore/abcdef0123456789", None),
    ("https://xhslink.com/synthetic", None),
])
def test_identity_uses_only_trusted_calibrated_display_links(snapshot, profile, url, expected):
    snapshot.xml = snapshot.xml.replace("synthetic/note-id", "synthetic/unrelated")
    snapshot.xml = snapshot.xml.replace("</hierarchy>",
                                        f'<node resource-id="synthetic/note-url" text="{url}"/>'
                                        '</hierarchy>')
    note = parse_note(snapshot, profile)
    assert note.note_id == expected
    assert note.canonical_url is None or "?" not in note.canonical_url


def test_conflicting_identity_is_not_trusted(snapshot, profile):
    snapshot.xml = snapshot.xml.replace("</hierarchy>",
                                        '<node resource-id="synthetic/note-url" '
                                        'text="https://www.xiaohongshu.com/'
                                        'explore/different12345678"/>'
                                        '</hierarchy>')
    note = parse_note(snapshot, profile)
    assert note.note_id is None
    assert "conflicting_identity_evidence" in note.warnings


def test_missing_identity_never_uses_fingerprint(snapshot, profile):
    snapshot.xml = snapshot.xml.replace("synthetic/note-id", "synthetic/unrelated")
    assert parse_note(snapshot, profile).note_id is None


def test_ocr_only_on_missing_calibrated_ui_region(snapshot, profile):
    calls = []

    class OCR:
        def read(self, snapshot, region):
            calls.append(region)
            return FieldValue(raw="SYNTHETIC OCR", status="low_quality", method="ocr",
                              confidence=0.2, region=list(region), reason="ocr_low_confidence")

    profile.fields["body"] = FieldRule(selector=Selector(resource_id="synthetic/body"),
                                       ocr_region=(10, 260, 1000, 600))
    assert parse_note(snapshot, profile, OCR()).fields["body"].method == "ui"
    assert calls == []
    snapshot.xml = snapshot.xml.replace('resource-id="synthetic/body"', 'resource-id="absent"')
    note = parse_note(snapshot, profile, OCR())
    assert calls == [(10, 260, 1000, 600)]
    assert note.fields["body"].confidence == 0.2
    assert not note.eligible


def test_candidates_video_filter_and_local_key(snapshot, profile):
    def card(text, bounds, video=False):
        badge = '<node resource-id="synthetic/video-badge"/>' if video else ""
        return (f'<node resource-id="synthetic/card" bounds="{bounds}">'
                f'<node text="SYNTHETIC {text}"/>{badge}</node>')

    snapshot.xml = ('<hierarchy>' + card("A", "[0,100][400,500]")
                    + card("B", "[400,100][800,500]", video=True) + '</hierarchy>')
    found = candidates(snapshot, profile)
    assert len(found) == 1
    key = found[0].key
    snapshot.xml = '<hierarchy>' + card("A", "[400,600][800,900]") + '</hierarchy>'
    assert candidates(snapshot, profile)[0].key == key


def test_candidate_tap_is_unique_inside_card_and_never_falls_back(snapshot, profile):
    profile.candidate_selector = Selector(class_name="card", clickable=True)
    profile.candidate_tap_selector = Selector(
        class_name="image", parent=Selector(class_name="cover"),
    )

    def view(targets):
        snapshot.xml = (
            '<hierarchy><node class="card" clickable="true" bounds="[0,100][400,700]">'
            f'<node class="cover">{targets}</node>'
            '<node class="footer" clickable="true" bounds="[0,650][400,700]">'
            '<node text="SYNTHETIC like button"/></node></node></hierarchy>'
        )

    target = '<node class="image" bounds="[0,100][400,600]"/>'
    view(target)
    assert candidates(snapshot, profile)[0].bounds == (0, 100, 400, 600)
    for bad in ("", target + target,
                '<node class="image" bounds="[0,100][401,600]"/>',
                '<node class="image" bounds="[0,100][400,1900]"/>',
                '<node class="image" bounds="invalid"/>',
                target.replace('class="image"', 'class="image" visible-to-user="false"')):
        view(bad)
        assert not candidates(snapshot, profile)


def test_unique_fields_reject_ambiguity_including_empty_nodes_and_no_ocr(snapshot, profile):
    class OCR:
        def read(self, snapshot, region):
            raise AssertionError("OCR must not hide an ambiguous UI selector")

    profile.fields["body"].many = False
    profile.fields["body"].ocr_region = (10, 260, 1000, 600)
    assert parse_note(snapshot, profile, OCR()).fields["body"].status == "present"
    for raw in ("", "SYNTHETIC unrelated comment"):
        original = snapshot.xml
        snapshot.xml = original.replace('</hierarchy>',
            f'<node resource-id="synthetic/body" text="{raw}"/></hierarchy>')
        note = parse_note(snapshot, profile, OCR())
        assert note.fields["body"].raw is None
        assert note.fields["body"].reason == "ambiguous_field_selector"
        assert not note.eligible
        snapshot.xml = original


def test_many_fields_keep_explicit_join_compatibility(snapshot, profile):
    profile.fields["body"].many = True
    before = parse_note(snapshot, profile).fields["body"].raw
    snapshot.xml = snapshot.xml.replace('</hierarchy>',
        '<node resource-id="synthetic/body" text="SYNTHETIC continuation"/></hierarchy>')
    assert parse_note(snapshot, profile).fields["body"].raw == before + "\nSYNTHETIC continuation"


@pytest.mark.parametrize("bounds", [
    "[10,100][1000,800]",  # touches the top occlusion boundary
    "[10,200][1000,900]",  # touches the bottom occlusion boundary
    "[0,200][1000,800]",   # touches the left boundary
    "[10,200][1080,800]",  # touches the right boundary
    "[10,90][1000,800]",   # partially above the visible region
    "[10,200][1000,950]",  # partially below it
    "[10,920][1000,1000]", # entirely outside it
    "invalid", "", None,
])
def test_body_visibility_never_asserts_incompleteness_or_discards_raw(
    snapshot, profile, bounds,
):
    import xml.etree.ElementTree as ET

    profile.body_visible_region = (0, 100, 1080, 900)
    root = ET.fromstring(snapshot.xml)
    node = root.find('.//node[@resource-id="synthetic/body"]')
    if bounds is None:
        node.attrib.pop("bounds", None)
    else:
        node.set("bounds", bounds)
    snapshot.xml = ET.tostring(root, encoding="unicode")
    note = parse_note(snapshot, profile)
    assert note.fields["body"].raw == node.get("text")
    assert "body_not_fully_visible" not in note.warnings
    assert note.body_complete is None and note.eligible


def test_visible_body_has_no_automatic_end_marker_requirement(snapshot, profile):
    import xml.etree.ElementTree as ET

    profile.body_visible_region = (0, 100, 1080, 900)
    root = ET.fromstring(snapshot.xml)
    node = root.find('.//node[@resource-id="synthetic/body"]')
    node.set("bounds", "[10,200][1000,800]")
    snapshot.xml = ET.tostring(root, encoding="unicode")
    assert parse_note(snapshot, profile).body_complete is None
    no_end = snapshot.xml.replace("synthetic/body-end", "synthetic/unrelated")
    original = snapshot.xml
    snapshot.xml = no_end
    assert not parse_note(snapshot, profile).body_complete
    snapshot.xml = original.replace('</hierarchy>',
        '<node resource-id="synthetic/body" text="SYNTHETIC second body" '
        'bounds="[10,300][1000,600]"/></hierarchy>')
    assert "body_not_fully_visible" not in parse_note(snapshot, profile).warnings
    assert not parse_note(snapshot, profile).body_complete


@pytest.mark.parametrize("target,accepted", [
    ("[0,200][400,500]", True),   # boundary equality is safe for taps
    ("[0,199][400,500]", False),  # partly under the top toolbar
    ("[0,200][400,701]", False), # partly under bottom controls
    ("[0,700][400,750]", False), # outside the safe region
    ("[0,200][400,248]", True),  # exactly the minimum visible height
    ("[0,200][400,247]", False),
])
def test_candidate_region_rejects_whole_target_without_clipping(
    snapshot, profile, target, accepted,
):
    profile.candidate_tap_selector = Selector(class_name="SYNTHETIC-cover")
    profile.candidate_tap_region = (0, 200, 400, 700)
    profile.candidate_min_height = 48
    snapshot.xml = (
        '<hierarchy><node resource-id="synthetic/card" bounds="[0,100][400,800]">'
        f'<node class="SYNTHETIC-cover" bounds="{target}"/>'
        '</node></hierarchy>'
    )
    found = candidates(snapshot, profile)
    assert bool(found) is accepted
    if found:
        import re
        assert found[0].bounds == tuple(map(int, re.findall(r"\d+", target)))


@pytest.mark.parametrize("raw,rejected", [
    ("SYNTHETIC author..", False), ("SYNTHETIC author…", False),
    ("SYNTHETIC .. author", False), ("SYNTHETIC author.. ", False),
])
def test_punctuation_suffix_keeps_raw_and_remains_readable(
    snapshot, profile, raw, rejected,
):
    import xml.etree.ElementTree as ET

    class OCR:
        def read(self, snapshot, region):
            raise AssertionError("Must not replace uncertain UI text with OCR")

    rule = profile.fields["author"]
    rule.reject_text_suffixes = ["..", "…"]
    rule.ocr_region = (10, 100, 1000, 200)
    root = ET.fromstring(snapshot.xml)
    root.find('.//node[@resource-id="synthetic/author"]').set("text", raw)
    snapshot.xml = ET.tostring(root, encoding="unicode")
    note = parse_note(snapshot, profile, OCR())
    assert note.fields["author"].raw == raw
    if rejected:
        assert note.fields["author"].status == "not_readable"
        assert note.fields["author"].reason == "possible_ui_truncation_or_placeholder"
        assert not note.eligible
    else:
        assert note.fields["author"].status == "present"
        assert note.eligible


@pytest.mark.parametrize("raw,rejected", [("SYNTHETIC .. placeholder", False),
                                          ("SYNTHETIC � placeholder", True)])
def test_only_substantive_rejection_preserves_uncertainty(snapshot, profile, raw, rejected):
    import xml.etree.ElementTree as ET

    profile.fields["body"].reject_text_contains = ["..", "�"]
    root = ET.fromstring(snapshot.xml)
    root.find('.//node[@resource-id="synthetic/body"]').set("text", raw)
    snapshot.xml = ET.tostring(root, encoding="unicode")
    note = parse_note(snapshot, profile)
    assert note.fields["body"].raw == raw
    assert note.fields["body"].status == ("not_readable" if rejected else "present")
    assert note.eligible is (not rejected)
    assert note.body_complete is None


def test_parser_uses_verified_resolver_text_without_rewriting_xml(snapshot, profile, monkeypatch):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(snapshot.xml)
    root.find('.//node[@resource-id="synthetic/author"]').set("text", "SYNTHETIC author..")
    snapshot.xml = ET.tostring(root, encoding="unicode")
    original_xml = snapshot.xml
    profile.fields["author"].reject_text_contains = [".."]
    calls = []

    def verified_resolver(root, node, observed, attribute="text"):
        # Unit-level boundary: the readback module separately verifies metadata.
        calls.append((node.get("resource-id"), attribute))
        assert observed is snapshot
        if node.get("resource-id") == "synthetic/author":
            return "SYNTHETIC author🥕", "ui_objinfo"
        return node.get(attribute, ""), "ui"

    monkeypatch.setattr(parser, "resolve_ui_text", verified_resolver)
    note = parse_note(snapshot, profile)
    assert note.fields["author"].raw == "SYNTHETIC author🥕"
    assert note.fields["author"].method == "ui_objinfo"
    assert note.eligible
    assert snapshot.xml == original_xml
    assert ("synthetic/author", "text") in calls


def test_invalid_supplement_cannot_replace_observed_punctuation(snapshot, profile):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(snapshot.xml)
    root.find('.//node[@resource-id="synthetic/author"]').set("text", "SYNTHETIC author..")
    snapshot.xml = ET.tostring(root, encoding="unicode")
    snapshot.metadata["ui_readbacks"] = {
        "schema_version": 1, "status": "verified", "xml_sha256": "invalid-SYNTHETIC-hash",
        "entries": [{"raw_value": "SYNTHETIC forged author🥕"}],
    }
    profile.fields["author"].reject_text_contains = [".."]
    note = parse_note(snapshot, profile)
    assert note.fields["author"].raw == "SYNTHETIC author.."
    assert note.fields["author"].method == "ui"
    assert note.fields["author"].status == "present"
    assert note.eligible


def test_resolved_text_is_not_judged_complete_from_bounds(snapshot, profile, monkeypatch):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(snapshot.xml)
    root.find('.//node[@resource-id="synthetic/body"]').set("bounds", "[10,100][1000,800]")
    snapshot.xml = ET.tostring(root, encoding="unicode")
    profile.body_visible_region = (0, 100, 1080, 900)

    def verified_resolver(root, node, observed, attribute="text"):
        if node.get("resource-id") == "synthetic/body":
            return "SYNTHETIC complete raw body🥕", "ui_objinfo"
        return node.get(attribute, ""), "ui"

    monkeypatch.setattr(parser, "resolve_ui_text", verified_resolver)
    note = parse_note(snapshot, profile)
    assert note.fields["body"].raw == "SYNTHETIC complete raw body🥕"
    assert note.fields["body"].method == "ui_objinfo"
    assert "body_not_fully_visible" not in note.warnings
    assert note.body_complete is None and note.eligible


def test_resolved_count_keeps_method_and_many_fields_mark_mixed_sources(
    snapshot, profile, monkeypatch,
):
    def verified_resolver(root, node, observed, attribute="text"):
        if node.get("resource-id") == "synthetic/likes":
            return "1.2万", "ui_objinfo"
        if node.get("resource-id") == "synthetic/tag" and node.get("text") == "#synthetic":
            return "#SYNTHETIC🥕", "ui_objinfo"
        return node.get(attribute, ""), "ui"

    monkeypatch.setattr(parser, "resolve_ui_text", verified_resolver)
    note = parse_note(snapshot, profile)
    assert note.fields["likes"].method == "ui_objinfo"
    assert note.fields["likes"].raw == "1.2万"
    assert note.fields["likes"].normalized == 12000
    assert note.fields["likes"].approximate
    assert note.fields["tags"].method == "ui_mixed"
    assert note.fields["tags"].raw == "#SYNTHETIC🥕\n#测试"


def test_content_description_is_passed_to_resolver(snapshot, profile, monkeypatch):
    profile.fields["published_at"].attribute = "content-desc"
    snapshot.xml = snapshot.xml.replace('</hierarchy>',
        '<node resource-id="synthetic/published-at" content-desc="SYNTHETIC date.."/>'
        '</hierarchy>')

    def verified_resolver(root, node, observed, attribute="text"):
        if node.get("resource-id") == "synthetic/published-at":
            assert attribute == "content-desc"
            return "SYNTHETIC displayed date", "ui_objinfo"
        return node.get(attribute, ""), "ui"

    monkeypatch.setattr(parser, "resolve_ui_text", verified_resolver)
    note = parse_note(snapshot, profile)
    assert note.fields["published_at"].raw == "SYNTHETIC displayed date"
    assert note.fields["published_at"].method == "ui_objinfo"
