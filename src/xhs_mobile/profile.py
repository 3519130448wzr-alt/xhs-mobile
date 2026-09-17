"""Explicit, evidence-calibrated selectors. No application selectors are guessed."""

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from xhs_mobile.domain import ProfileError, Snapshot, UIState

PAGES = {
    "home", "search", "results", "filter", "detail", "video", "loading", "login",
    "verification", "restricted", "rate_limited", "share",
}
ALERT_PAGES = {"login", "verification", "restricted", "rate_limited"}
ATTRIBUTES = {
    "resource_id": "resource-id", "text": "text", "content_desc": "content-desc",
    "class_name": "class", "package_name": "package",
}
BOOLEAN_ATTRIBUTES = {"clickable", "focused", "selected"}
Bounds = tuple[int, int, int, int]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Selector(StrictModel):
    """Exact attributes with an optional chain of immediate visible parents."""

    resource_id: str | None = None
    text: str | None = None
    content_desc: str | None = None
    content_desc_prefix: str | None = None
    class_name: str | None = None
    package_name: str | None = None
    clickable: bool | None = None
    focused: bool | None = None
    selected: bool | None = None
    xml_index: int | None = Field(default=None, ge=0, strict=True)
    parent: "Selector | None" = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.content_desc_prefix is not None and not self.content_desc_prefix.strip():
            raise ValueError("content_desc_prefix must be a nonempty literal prefix")
        if not (any(getattr(self, name) for name in ATTRIBUTES)
                or any(getattr(self, name) is not None for name in BOOLEAN_ATTRIBUTES)
                or self.content_desc_prefix is not None):
            raise ValueError("a selector requires at least one nonempty exact attribute")
        return self


class PageRule(StrictModel):
    activity: str | None = None
    all: list[Selector] = Field(default_factory=list)
    any: list[Selector] = Field(default_factory=list)
    none: list[Selector] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.all and not self.any:
            raise ValueError("a page rule requires a positive observable marker")
        if self.activity is not None and not self.activity.strip():
            raise ValueError("activity must be a nonempty exact name")
        return self


class FieldRule(StrictModel):
    platform_topic: bool = False
    selector: Selector | None = None
    attribute: Literal["text", "content-desc"] = "text"
    join: str = "\n"
    many: bool = True
    reject_text_suffixes: list[str] = Field(default_factory=list)
    reject_text_contains: list[str] = Field(default_factory=list)
    ocr_region: Bounds | None = None

    @model_validator(mode="after")
    def valid_source(self):
        if self.selector is None and self.ocr_region is None:
            raise ValueError("field needs a selector or a calibrated OCR region")
        if any(suffix == "" for suffix in self.reject_text_suffixes):
            raise ValueError("reject_text_suffixes cannot contain an empty suffix")
        if any(value == "" for value in self.reject_text_contains):
            raise ValueError("reject_text_contains cannot contain an empty value")
        if self.ocr_region is not None:
            x1, y1, x2, y2 = self.ocr_region
            if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
                raise ValueError("OCR region must be a positive rectangle")
        return self


class Profile(StrictModel):
    name: str = "uncalibrated"
    verified: bool = False
    synthetic: bool = False
    app_package: str = ""
    app_version: str = ""
    reference_resolution: tuple[int, int] = (0, 0)
    calibration_evidence: list[str] = Field(default_factory=list)
    search_input_text_prefix: str = ""
    pages: dict[str, PageRule] = Field(default_factory=dict)
    actions: dict[str, Selector] = Field(default_factory=dict)
    candidate_selector: Selector | None = None
    candidate_tap_selector: Selector | None = None
    candidate_tap_region: Bounds | None = None
    candidate_min_height: int = Field(default=1, ge=1)
    candidate_video_marker: Selector | None = None
    image_text_selected_marker: Selector | None = None
    detail_body_swipes: int = Field(default=0, ge=0, le=5)
    fields: dict[str, FieldRule] = Field(default_factory=dict)
    body_complete_marker: Selector | None = None
    body_visible_region: Bounds | None = None
    body_incomplete_markers: list[Selector] = Field(default_factory=list)
    title_not_displayed_marker: Selector | None = None

    @model_validator(mode="after")
    def coherent(self):
        if set(self.pages) - PAGES:
            raise ValueError(f"unsupported page names: {sorted(set(self.pages) - PAGES)}")
        if self.verified:
            if not self.app_package or not self.app_version:
                raise ValueError("verified profile needs exact app package and version")
            if min(self.reference_resolution) <= 0:
                raise ValueError("verified profile needs positive reference_resolution")
            if not self.calibration_evidence or not all(
                v.strip() for v in self.calibration_evidence
            ):
                raise ValueError("verified profile needs calibration_evidence paths")
            missing = {"search_entry", "search_input", "search_submit"} - self.actions.keys()
            if missing:
                raise ValueError(f"verified profile missing actions: {sorted(missing)}")
            if not {"home", "search", "results", "detail"} <= self.pages.keys():
                raise ValueError("verified profile needs home, search, results and detail markers")
            if self.candidate_selector is None:
                raise ValueError("verified profile needs candidate markers")
            if not {"body", "author"} <= self.fields.keys():
                raise ValueError("verified profile needs body and author extraction rules")
            if "title" not in self.fields and self.title_not_displayed_marker is None:
                raise ValueError("verified profile needs a title or explicit title-absent marker")
            if "filter_entry" in self.actions:
                if "filter" not in self.pages:
                    raise ValueError("filter_entry needs a calibrated filter page")
                filter_actions = {"image_text_filter", "filter_confirm"} - self.actions.keys()
                if filter_actions:
                    raise ValueError(f"filter_entry missing actions: {sorted(filter_actions)}")
                if self.image_text_selected_marker is None:
                    raise ValueError("filter_entry needs image_text_selected_marker")
        width, height = self.reference_resolution
        if height > 0 and self.candidate_min_height > height:
            raise ValueError("candidate_min_height exceeds reference resolution height")
        if self.candidate_tap_region is not None:
            x1, y1, x2, y2 = self.candidate_tap_region
            if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
                raise ValueError("candidate_tap_region must be a positive rectangle")
            if width > 0 and height > 0 and (x2 > width or y2 > height):
                raise ValueError("candidate_tap_region extends beyond reference resolution")
        if self.body_visible_region is not None:
            x1, y1, x2, y2 = self.body_visible_region
            if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
                raise ValueError("body_visible_region must be a positive rectangle")
            if width > 0 and height > 0 and (x2 > width or y2 > height):
                raise ValueError("body_visible_region extends beyond reference resolution")
        if width > 0 and height > 0:
            for rule in self.fields.values():
                if rule.ocr_region and (rule.ocr_region[2] > width or rule.ocr_region[3] > height):
                    raise ValueError("OCR region extends beyond reference resolution")
        return self


def load_profile(path: str | Path, require_verified: bool = True) -> Profile:
    try:
        with Path(path).open("rb") as handle:
            profile = Profile.model_validate(tomllib.load(handle))
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ProfileError(f"Cannot load profile {path}: {exc}") from exc
    if require_verified and (not profile.verified or profile.synthetic):
        raise ProfileError("Production collection requires a verified, non-synthetic profile")
    if require_verified:
        for evidence in profile.calibration_evidence:
            evidence_path = Path(evidence)
            if not evidence_path.is_absolute():
                evidence_path = Path(path).resolve().parent / evidence_path
            try:
                with evidence_path.open("rb") as handle:
                    if not handle.read(1):
                        raise ProfileError(f"Calibration evidence is empty: {evidence_path}")
            except OSError as exc:
                raise ProfileError(f"Cannot read calibration evidence: {evidence_path}") from exc
    return profile


def validate_snapshot(snapshot: Snapshot | UIState, profile: Profile) -> None:
    """Reject version/size drift before interpreting or acting on a screenshot."""
    if not profile.verified:
        raise ProfileError("Profile is uncalibrated; use doctor/snapshot before collection")
    metadata = snapshot.metadata
    if metadata.get("app_version") != profile.app_version:
        raise ProfileError("App version differs from calibrated profile or is unavailable")
    resolution = metadata.get("resolution")
    if (not isinstance(resolution, (tuple, list))
            or tuple(resolution) != profile.reference_resolution):
        raise ProfileError("Screen resolution differs from calibrated profile or is unavailable")
    if metadata.get("app_package") not in (None, profile.app_package):
        raise ProfileError("App package differs from calibrated profile")
    if metadata.get("configured_package") not in (None, "", profile.app_package):
        raise ProfileError("Configured App package differs from calibrated profile")
    if "package" in metadata and metadata["package"] != profile.app_package:
        raise ProfileError("Calibrated App is not the confirmed foreground package")
    if not profile.synthetic:
        foreground = metadata.get("package", metadata.get("app_package"))
        if foreground != profile.app_package:
            raise ProfileError("Cannot confirm calibrated App is in the foreground")


def xml_root(snapshot: Snapshot | UIState) -> ET.Element:
    if len(snapshot.xml.encode("utf-8")) > 8 * 1024 * 1024:
        raise ProfileError("UI XML exceeds 8 MiB limit")
    if "<!DOCTYPE" in snapshot.xml.upper() or "<!ENTITY" in snapshot.xml.upper():
        raise ProfileError("DTD and XML entities are not permitted in UI snapshots")
    try:
        return ET.fromstring(snapshot.xml)
    except ET.ParseError as exc:
        raise ProfileError(f"Invalid UI XML: {exc}") from exc


def matches(
    node: ET.Element, selector: Selector, parents: dict[ET.Element, ET.Element] | None = None
) -> bool:
    if node.get("visible-to-user") == "false":
        return False
    exact = all(
        node.get(xml_name) == value
        for name, xml_name in ATTRIBUTES.items()
        if (value := getattr(selector, name)) is not None
    )
    exact = exact and all(
        node.get(name) == ("true" if value else "false")
        for name in BOOLEAN_ATTRIBUTES
        if (value := getattr(selector, name)) is not None
    )
    if selector.content_desc_prefix is not None:
        exact = exact and node.get("content-desc", "").startswith(selector.content_desc_prefix)
    if selector.xml_index is not None:
        exact = exact and node.get("index") == str(selector.xml_index)
    if not exact or selector.parent is None:
        return exact
    parent = parents.get(node) if parents is not None else None
    return parent is not None and matches(parent, selector.parent, parents)


def matching(root: ET.Element, selector: Selector | None) -> list[ET.Element]:
    if selector is None:
        return []
    # Exclude a hidden ancestor as well as a hidden node.
    result: list[ET.Element] = []
    parents = {child: parent for parent in root.iter() for child in parent}

    pending = [root]
    while pending:
        node = pending.pop()
        if node.get("visible-to-user") == "false":
            continue
        if matches(node, selector, parents):
            result.append(node)
        pending.extend(reversed(list(node)))
    return result


def has(root: ET.Element, selector: Selector | None) -> bool:
    return bool(matching(root, selector))


def bounds_for(node: ET.Element, resolution: tuple[int, int]) -> Bounds | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        return None
    bounds = tuple(int(value) for value in match.groups())
    x1, y1, x2, y2 = bounds
    if not (0 <= x1 < x2 <= resolution[0] and 0 <= y1 < y2 <= resolution[1]):
        return None
    return bounds


def classify(snapshot: Snapshot | UIState, profile: Profile) -> str:
    validate_snapshot(snapshot, profile)
    root = xml_root(snapshot)
    detected = {
        name for name, rule in profile.pages.items()
        if all(has(root, selector) for selector in rule.all)
        and (not rule.any or any(has(root, selector) for selector in rule.any))
        and not any(has(root, selector) for selector in rule.none)
        and (rule.activity is None or snapshot.metadata.get("activity") == rule.activity)
    }
    alerts = detected & ALERT_PAGES
    if alerts:
        return next(iter(alerts)) if len(alerts) == 1 else "unknown"
    return next(iter(detected)) if len(detected) == 1 else "unknown"


def locate(snapshot: Snapshot | UIState, profile: Profile, action: str) -> Bounds | None:
    validate_snapshot(snapshot, profile)
    nodes = matching(xml_root(snapshot), profile.actions.get(action))
    # Ambiguous action targets are unsafe, even if only one has readable bounds.
    return bounds_for(nodes[0], profile.reference_resolution) if len(nodes) == 1 else None
