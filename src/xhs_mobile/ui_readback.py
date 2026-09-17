"""Bounded, read-only objInfo evidence for lossy uiautomator XML text.

OpenATX u2.jar 0.4.0 replaces each UTF-16 surrogate with a dot in its XML
dumper. Original XML is immutable. Only independently read, uniquely mapped,
stable objInfo values may supplement it; ordinary literal dots are not guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

from .domain import Snapshot, utcnow

MAX_TARGETS = 32
MAX_GROUPS = 8
MAX_RESULTS = 128
MAX_BYTES = 2 * 1024 * 1024
ATTRIBUTES = {"text": "text", "content-desc": "contentDescription"}


def xml_sanitized(value: str) -> str:
    """Reproduce u2.jar 0.4.0's sanitizer for comparison, never for restoration."""
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    result = []
    for index in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[index:index + 2], "little")
        invalid = any(lo <= unit <= hi for lo, hi in (
            (0, 8), (11, 12), (14, 31), (127, 132), (134, 159),
            (0xD800, 0xDFFF), (0xFDD0, 0xFDDF), (0xFFFE, 0xFFFF),
        ))
        result.append("." if invalid else chr(unit))
    return "".join(result)


def _tree(xml: str) -> ET.Element:
    if not isinstance(xml, str) or len(xml.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Invalid or oversized hierarchy")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("DTD/entities are not supported")
    return ET.fromstring(xml)


def _nodes(root):
    pending = [(root, [])]
    while pending:
        node, path = pending.pop()
        if node.get("visible-to-user") == "false":
            continue
        if node.tag == "node":
            yield node, path
        pending.extend((child, [*path, index]) for index, child in reversed(list(enumerate(node))))


def _selector(node):
    return {
        "packageName": node.get("package", ""), "resourceId": node.get("resource-id", ""),
        "className": node.get("class", ""),
    }


def _members(root, selector):
    return [{"node_path": path, "node_attrs": dict(node.attrib)} for node, path in _nodes(root)
            if _selector(node) == selector]


def _rect(node, resolution):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if match is None:
        return None
    bounds = tuple(map(int, match.groups()))
    left, top, right, bottom = bounds
    if not (0 <= left < right <= resolution[0] and 0 <= top < bottom <= resolution[1]):
        return None
    return bounds


def _matches_info(info, node, resolution, *, require_resource=True):
    if not isinstance(info, dict) or info.get("packageName") != node.get("package"):
        return False
    if info.get("className") != node.get("class"):
        return False
    expected_resource = node.get("resource-id")
    resources = {expected_resource} if require_resource else {expected_resource, "", None}
    if info.get("resourceName") not in resources:
        return False
    visible = info.get("visibleBounds")
    if not isinstance(visible, dict):
        return False
    values = tuple(visible.get(key) for key in ("left", "top", "right", "bottom"))
    return all(type(value) is int for value in values) and values == _rect(node, resolution)


def _unique_xml_node(root, selector, info, resolution):
    return sum(1 for node, _ in _nodes(root) if _selector(node) == selector
               and _matches_info(info, node, resolution, require_resource=False)) == 1


def _changed_value(info, node, attribute):
    value = info.get(ATTRIBUTES[attribute])
    if not isinstance(value, str):
        return None
    # Isolated surrogate strings cannot be recorded as exact UTF-8 evidence.
    value.encode("utf-8", errors="strict")
    original = node.get(attribute, "")
    if value == original or ".." not in original or xml_sanitized(value) != original:
        return None
    return value


def _same_optional_text(left, right):
    # Legacy UiObject.safeStringReturn converts an absent CharSequence to "";
    # UiObject2 preserves null. Preserve both raw RPC values in the evidence,
    # while treating only this specific empty-value representation as equal.
    if left is not None and not isinstance(left, str):
        return False
    if right is not None and not isinstance(right, str):
        return False
    return ("" if left is None else left) == ("" if right is None else right)


def unavailable_readbacks(xml: str, package: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "status": "unavailable", "configured_package": package,
        "xml_sha256": hashlib.sha256(xml.encode("utf-8")).hexdigest(),
        "entries": [], "groups": [], "reasons": [reason],
    }


def capture_hierarchy(device: Any, configured_package: str) -> dict[str, Any]:
    """Read XML and bounded raw node info; recheck XML only for supplements.

    A failed supplement keeps the readable hierarchy and an unavailable marker.
    Never click or type. The enclosing worker's process timeout also bounds
    third-party RPC waits.
    """
    started = utcnow().isoformat()
    before_xml = device.dump_hierarchy(compressed=False)
    metadata = unavailable_readbacks(before_xml, configured_package, "readback_not_completed")
    after_xml = before_xml
    try:
        root = _tree(before_xml)
        targets = [(node, path) for node, path in _nodes(root)
                   if node.get("package") == configured_package and configured_package
                   and node.get("password") != "true"
                   and any(".." in node.get(attribute, "") for attribute in ATTRIBUTES)]
        if not targets:
            metadata.update(status="not_needed", reasons=[])
            return {"xml": before_xml, "readbacks": metadata}
        selectors = list({tuple(_selector(node).items()): _selector(node)
                          for node, _ in targets}.values())
        if len(targets) > MAX_TARGETS or len(selectors) > MAX_GROUPS:
            metadata["reasons"] = ["readback_budget_exceeded"]
            return {"xml": before_xml, "readbacks": metadata}
        if any(not all(selector.values()) for selector in selectors):
            metadata["reasons"] = ["missing_node_identity"]
            return {"xml": before_xml, "readbacks": metadata}
        device_info = device.info
        resolution = [device_info["displayWidth"], device_info["displayHeight"]]
        if not all(type(value) is int and value > 0 for value in resolution):
            raise ValueError("Invalid display dimensions")
        groups = []
        result_count = 0
        pending_entries = []
        for selector in selectors:
            before_members = _members(root, selector)
            if len(before_members) > MAX_RESULTS:
                raise OverflowError("readback_budget_exceeded")
            if any(member["node_attrs"].get("password") == "true" for member in before_members):
                raise ValueError("Readback group contains a password field")
            read_started = utcnow().isoformat()
            objects = device(**selector).info_list()
            if not isinstance(objects, list):
                raise ValueError("Invalid objInfo list")
            result_count += len(objects)
            if result_count > MAX_RESULTS:
                raise OverflowError("readback_budget_exceeded")
            related = [info for info in objects if any(
                _selector(node) == selector and _matches_info(
                    info, node, resolution, require_resource=False
                ) for node, _ in targets
            )]
            group = {
                "selector": selector, "before_nodes": before_members, "objects": related,
                "returned_result_count": len(objects),
                "read_started_at": read_started, "read_finished_at": utcnow().isoformat(),
            }
            groups.append(group)
            for node, path in targets:
                if _selector(node) != selector or _rect(node, resolution) is None:
                    continue
                matches = [item for item in objects
                           if _matches_info(item, node, resolution, require_resource=False)]
                if len(matches) != 1:
                    continue
                info = matches[0]
                if not _unique_xml_node(root, selector, info, resolution):
                    continue
                # Literal dots also match the initial loss-candidate scan. If
                # this independent read cannot supplement either field, an
                # additional identity RPC cannot make it acceptable evidence.
                if not any(_changed_value(info, node, attribute) is not None
                           for attribute in ATTRIBUTES):
                    continue
                verification_selector = None
                if not info.get("resourceName"):
                    # info_list() uses legacy UiObject, which omits resourceName.
                    # Verify it through UiObject2 using already observed raw text.
                    raw = info.get("text")
                    if not isinstance(raw, str) or not raw:
                        continue
                    if result_count >= MAX_RESULTS:
                        raise OverflowError("readback_budget_exceeded")
                    verification_selector = {**selector, "text": raw}
                    info = device(**verification_selector).info
                    result_count += 1
                if not _matches_info(info, node, resolution):
                    continue
                for attribute in ATTRIBUTES:
                    value = _changed_value(info, node, attribute)
                    if value is not None:
                        pending_entries.append({
                            "node_path": path, "node_attrs": dict(node.attrib),
                            "attribute": attribute, "xml_value": node.get(attribute, ""),
                            "raw_value": value, "selector": selector, "raw_objinfo": info,
                            "verification_selector": verification_selector,
                            "read_started_at": read_started,
                            "read_finished_at": utcnow().isoformat(),
                        })
            if len(json.dumps(groups, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
                raise OverflowError("readback_budget_exceeded")
        if not pending_entries:
            # No text will be replaced, so a second full tree cannot validate
            # any supplement. Keep the first XML as-is; do not claim the page
            # was stable while reading the optional node info.
            metadata.update(
                resolution=resolution, groups=groups, result_count=result_count,
                before_xml_sha256=metadata["xml_sha256"],
                reasons=["no_unique_lossless_mapping"],
                stability_check="not_performed_no_supplement",
                started_at=started, finished_at=utcnow().isoformat(),
            )
            if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
                raise OverflowError("readback_budget_exceeded")
            return {"xml": before_xml, "readbacks": metadata}
        after_xml = device.dump_hierarchy(compressed=False)
        after_root = _tree(after_xml)
        for group in groups:
            group["after_nodes"] = _members(after_root, group["selector"])
            if group["before_nodes"] != group["after_nodes"]:
                metadata = unavailable_readbacks(after_xml, configured_package, "page_changed")
                return {"xml": after_xml, "readbacks": metadata}
        after_targets = {tuple(path): dict(node.attrib) for node, path in _nodes(after_root)}
        entries = [entry for entry in pending_entries
                   if after_targets.get(tuple(entry["node_path"])) == entry["node_attrs"]]
        metadata = {
            "schema_version": 1, "status": "verified" if entries else "unavailable",
            "configured_package": configured_package, "resolution": resolution,
            "before_xml_sha256": hashlib.sha256(before_xml.encode("utf-8")).hexdigest(),
            "xml_sha256": hashlib.sha256(after_xml.encode("utf-8")).hexdigest(),
            "entries": entries, "groups": groups,
            "reasons": [] if entries else ["no_unique_lossless_mapping"],
            "started_at": started, "finished_at": utcnow().isoformat(),
            "result_count": result_count,
        }
        if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            raise OverflowError("readback_budget_exceeded")
    except Exception as exc:
        reason = "readback_budget_exceeded" if isinstance(exc, OverflowError) else (
            "objinfo_unavailable_" + type(exc).__name__
        )
        metadata = unavailable_readbacks(after_xml, configured_package, reason)
    return {"xml": after_xml, "readbacks": metadata}


def resolve_ui_text(
    root: ET.Element, node: ET.Element, snapshot: Snapshot, attribute: str = "text"
) -> tuple[str, str]:
    """Return validated objInfo text or unchanged XML text, failing closed."""
    fallback = (node.get(attribute, ""), "ui")
    try:
        if attribute not in ATTRIBUTES or ".." not in fallback[0]:
            return fallback
        metadata = snapshot.metadata.get("ui_readbacks")
        if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
            return fallback
        if metadata.get("status") != "verified":
            return fallback
        if metadata.get("xml_sha256") != hashlib.sha256(snapshot.xml.encode("utf-8")).hexdigest():
            return fallback
        if metadata.get("screenshot_sha256") != hashlib.sha256(snapshot.png).hexdigest():
            return fallback
        resolution = snapshot.metadata.get("resolution")
        if (not isinstance(resolution, (list, tuple)) or len(resolution) != 2
                or not all(type(value) is int and value > 0 for value in resolution)
                or list(resolution) != metadata.get("resolution")):
            return fallback
        package = metadata.get("configured_package")
        if (not package or package != node.get("package")
                or snapshot.metadata.get("configured_package") != package
                or snapshot.metadata.get("package") != package):
            return fallback
        entries, groups = metadata["entries"], metadata["groups"]
        if not isinstance(entries, list) or not isinstance(groups, list):
            return fallback
        if len(entries) > MAX_TARGETS * 2 or len(groups) > MAX_GROUPS:
            return fallback
        if (type(metadata.get("result_count")) is not int
                or not 0 <= metadata["result_count"] <= MAX_RESULTS):
            return fallback
        if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            return fallback
        current = next((path for candidate, path in _nodes(root) if candidate is node), None)
        if current is None or node.get("password") == "true":
            return fallback
        actual = {tuple(path): dict(candidate.attrib)
                  for candidate, path in _nodes(_tree(snapshot.xml))}
        if actual.get(tuple(current)) != dict(node.attrib):
            return fallback
        selected = [entry for entry in entries
                    if entry.get("node_path") == current and entry.get("attribute") == attribute]
        if len(selected) != 1:
            return fallback
        entry = selected[0]
        if entry.get("node_attrs") != dict(node.attrib) or entry.get("xml_value") != fallback[0]:
            return fallback
        selector = _selector(node)
        if not all(selector.values()) or entry.get("selector") != selector:
            return fallback
        selected_groups = [group for group in groups if group.get("selector") == selector]
        if len(selected_groups) != 1:
            return fallback
        group = selected_groups[0]
        members = _members(root, selector)
        if group.get("before_nodes") != members or group.get("after_nodes") != members:
            return fallback
        if any(type(group.get("returned_result_count")) is not int
               or not len(group["objects"]) <= group["returned_result_count"] <= MAX_RESULTS
               for group in groups):
            return fallback
        if sum(group["returned_result_count"] for group in groups) > metadata["result_count"]:
            return fallback
        objects = [info for info in group["objects"]
                   if _matches_info(info, node, resolution, require_resource=False)]
        if len(objects) != 1:
            return fallback
        info = entry.get("raw_objinfo")
        if not _matches_info(info, node, resolution):
            return fallback
        if not _unique_xml_node(root, selector, info, resolution):
            return fallback
        if info != objects[0]:
            expected = {**selector, "text": objects[0].get("text")}
            if (objects[0].get("resourceName") or entry.get("verification_selector") != expected
                    or info.get("text") != objects[0].get("text")
                    or not _same_optional_text(
                        info.get("contentDescription"), objects[0].get("contentDescription")
                    )):
                return fallback
        value = _changed_value(info, node, attribute)
        if value is None or value != entry.get("raw_value"):
            return fallback
        return value, "ui_objinfo"
    except (TypeError, ValueError, KeyError, AttributeError, ET.ParseError):
        return fallback
