"""Read observed UI values, preserving uncertainty and source provenance."""

import hashlib
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlsplit

from xhs_mobile.domain import Candidate, FieldValue, ParsedNote, Snapshot, UIState
from xhs_mobile.profile import (
    Bounds,
    Profile,
    bounds_for,
    classify,
    has,
    matching,
    validate_snapshot,
    xml_root,
)
from xhs_mobile.ui_readback import resolve_ui_text

BASE_FIELDS = ("title", "body", "author", "published_at", "likes", "favorites", "comments", "tags")
COUNT_FIELDS = {
    "likes", "favorites", "collects", "comments", "shares", "like_count", "collect_count",
    "comment_count",
}


class OCRReader(Protocol):
    def read(self, snapshot: Snapshot, region: Bounds) -> FieldValue: ...


def parse_count(raw: str | None) -> FieldValue:
    """Approximate display units stay approximate; invalid text never becomes zero."""
    if raw is None or not raw.strip():
        return FieldValue(raw=raw, reason="empty_count")
    value = raw.strip()
    # Grouping is accepted only when every comma group is exactly three digits.
    pattern = r"([0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+)(万|亿|[kKmMwW])?(\+)?"
    match = re.fullmatch(pattern, value)
    if match is None:
        return FieldValue(raw=raw, status="present", method="ui", reason="unparsed_count")
    number, unit, plus = match.groups()
    multiplier = {None: 1, "万": 10000, "亿": 100000000, "k": 1000,
                  "m": 1000000, "w": 10000}[unit.lower() if unit else None]
    try:
        scaled = Decimal(number.replace(",", "")) * multiplier
    except InvalidOperation:
        return FieldValue(raw=raw, status="present", method="ui", reason="unparsed_count")
    if scaled != scaled.to_integral_value():
        return FieldValue(raw=raw, status="present", method="ui", reason="noninteger_count")
    return FieldValue(raw=raw, status="present", method="ui", normalized=int(scaled),
                      approximate=bool(unit or plus))


def candidates(snapshot: Snapshot | UIState, profile: Profile) -> list[Candidate]:
    validate_snapshot(snapshot, profile)
    root = xml_root(snapshot)
    result = []
    for node in matching(root, profile.candidate_selector):
        if has(node, profile.candidate_video_marker):
            continue
        bounds = bounds_for(node, profile.reference_resolution)
        if bounds is None:
            continue
        if profile.candidate_tap_selector is not None:
            targets = matching(node, profile.candidate_tap_selector)
            if len(targets) != 1:
                continue
            tap_bounds = bounds_for(targets[0], profile.reference_resolution)
            if tap_bounds is None or not (
                bounds[0] <= tap_bounds[0] < tap_bounds[2] <= bounds[2]
                and bounds[1] <= tap_bounds[1] < tap_bounds[3] <= bounds[3]
            ):
                continue
            bounds = tap_bounds
        if bounds[3] - bounds[1] < profile.candidate_min_height:
            continue
        region = profile.candidate_tap_region
        if region is not None and not (
            region[0] <= bounds[0] < bounds[2] <= region[2]
            and region[1] <= bounds[1] < bounds[3] <= region[3]
        ):
            continue
        # This hash suggests duplicate cards within this scan; it is never a note identity.
        displayed = "\n".join(
            f"{child.get('text', '')}\t{child.get('content-desc', '')}"
            for child in node.iter() if child.get("visible-to-user") != "false"
        )
        key = hashlib.sha256(displayed.encode("utf-8")).hexdigest()
        result.append(Candidate(key=key, bounds=bounds))
    return result


def _canonical_url(raw: str | None) -> tuple[str, str] | None:
    if not raw:
        return None
    identities = set()
    for candidate in re.findall(r"https?://[^\s<>\"']+", raw):
        try:
            parsed = urlsplit(candidate.rstrip("。，,;；)）]"))
            if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                continue
            host = (parsed.hostname or "").lower()
            if host not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
                continue
            path = re.fullmatch(
                r"/(?:explore|discovery/item)/([A-Za-z0-9_-]{8,128})/?", parsed.path
            )
            if path:
                note_id = path.group(1)
                identities.add((note_id, f"https://www.xiaohongshu.com/explore/{note_id}"))
        except ValueError:
            continue
    return next(iter(identities)) if len(identities) == 1 else None


def parse_note(snapshot: Snapshot, profile: Profile, ocr: OCRReader | None = None) -> ParsedNote:
    validate_snapshot(snapshot, profile)
    root = xml_root(snapshot)
    page = classify(snapshot, profile)
    fields: dict[str, FieldValue] = {}
    topics: list[str] = []
    topic_sources: list[dict] = []
    for name in dict.fromkeys((*BASE_FIELDS, *profile.fields)):
        rule = profile.fields.get(name)
        if name == "tags" and (rule is None or not rule.platform_topic):
            fields[name] = FieldValue(reason="platform_topics_unrecognized")
            continue
        if rule is None:
            fields[name] = FieldValue(reason="field_not_calibrated")
            continue
        nodes = matching(root, rule.selector)
        if not rule.many and len(nodes) > 1:
            fields[name] = FieldValue(reason="ambiguous_field_selector")
            continue
        resolved = [resolve_ui_text(root, node, snapshot, rule.attribute) for node in nodes]
        readable = [(raw, method) for raw, method in resolved if raw.strip()]
        raw_values = [raw for raw, _ in readable]
        if raw_values:
            methods = {method for _, method in readable}
            fields[name] = FieldValue(
                raw=rule.join.join(raw_values), status="present",
                method=next(iter(methods)) if len(methods) == 1 else "ui_mixed",
            )
        elif rule.ocr_region is not None and ocr is not None:
            fields[name] = ocr.read(snapshot, rule.ocr_region)
        else:
            reason = (
                "ocr_unavailable" if rule.ocr_region is not None else "selector_missing_or_empty"
            )
            fields[name] = FieldValue(reason=reason)
        # Punctuation cannot prove truncation. Keep explicit replacement-character
        # and other calibrated unreadability checks, but never infer completeness.
        def substantive(value: str) -> bool:
            return bool(value.strip(".…。． \t\r\n"))

        if fields[name].raw is not None and (
            any(fields[name].raw.endswith(suffix) for suffix in rule.reject_text_suffixes
                if substantive(suffix))
            or any(value in fields[name].raw for value in rule.reject_text_contains
                   if substantive(value))
        ):
            fields[name] = replace(
                fields[name], status="not_readable", reason="possible_ui_truncation_or_placeholder"
            )
            continue
        if name == "tags" and fields[name].status == "present":
            # Each independently calibrated platform component is a topic. Never
            # split a body TextView or infer topics from ordinary hashtag text.
            for raw, method in readable:
                if raw not in topics:
                    topics.append(raw)
                    topic_sources.append({"name": raw, "method": method, "evidence_ref": None})
            fields[name] = replace(fields[name], raw=rule.join.join(topics))
        if name in COUNT_FIELDS and fields[name].raw:
            parsed = parse_count(fields[name].raw)
            fields[name] = replace(fields[name], normalized=parsed.normalized,
                                   approximate=parsed.approximate, reason=parsed.reason
                                   or fields[name].reason)

    warnings = []
    if fields["title"].status != "present" and has(root, profile.title_not_displayed_marker):
        fields["title"] = FieldValue(status="not_displayed", method="ui",
                                     reason="explicit_title_absence_marker")
    if page != "detail":
        warnings.append(f"not_image_text_detail:{page}")

    # Identity may come only from calibrated fields, never title/author/OCR guesses.
    note_id = None
    canonical_url = None
    identity_source = None
    explicit = fields.get("note_id", FieldValue())
    if explicit.method == "ui" and explicit.raw:
        if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", explicit.raw.strip()):
            note_id = explicit.raw.strip()
            identity_source = "calibrated_ui_note_id"
        else:
            warnings.append("invalid_explicit_note_id")
    link_field = fields.get("note_url", fields.get("url", FieldValue()))
    identity_from_url = _canonical_url(link_field.raw) if link_field.method == "ui" else None
    if identity_from_url:
        linked_id, link = identity_from_url
        if note_id and note_id != linked_id:
            warnings.append("conflicting_identity_evidence")
            note_id = None
            identity_source = None
        else:
            note_id, canonical_url = linked_id, link
            identity_source = "displayed_xiaohongshu_url"

    return ParsedNote(fields=fields, note_id=note_id, canonical_url=canonical_url,
                      identity_source=identity_source,
                      content_type="image_text" if page == "detail" else page,
                      time_kind=page_time_kind(fields["published_at"]),
                      topics_status="confirmed" if topics else "unrecognized",
                      topics=topics, topic_sources=topic_sources, warnings=warnings)


def page_time_kind(value: FieldValue | dict) -> str:
    """Describe the visible label without converting relative text into dates."""
    raw = value.get("raw") if isinstance(value, dict) else value.raw
    status = value.get("status") if isinstance(value, dict) else value.status
    if status != "present" or not raw:
        return "not_readable"
    return "edited" if str(raw).lstrip().startswith("编辑于") else "published"
