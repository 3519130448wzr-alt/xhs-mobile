"""Offline inspection of partial rules without promoting them to a verified profile."""

from xhs_mobile.domain import ProfileError, Snapshot
from xhs_mobile.profile import Profile, bounds_for, matching, xml_root


def missing_production_requirements(profile: Profile) -> list[str]:
    """Report structural gaps only; this is not production or evidence validation."""
    missing = []
    if not profile.verified:
        missing.append("verified=true")
    if profile.synthetic:
        missing.append("synthetic=false")
    for name in ("app_package", "app_version"):
        if not getattr(profile, name).strip():
            missing.append(name)
    if min(profile.reference_resolution) <= 0:
        missing.append("reference_resolution")
    if not profile.calibration_evidence or not all(
        item.strip() for item in profile.calibration_evidence
    ):
        missing.append("calibration_evidence")
    missing.extend(
        f"pages.{name}" for name in ("home", "search", "results", "detail")
        if name not in profile.pages
    )
    missing.extend(
        f"actions.{name}" for name in ("search_entry", "search_input", "search_submit")
        if name not in profile.actions
    )
    missing.extend(
        name for name in ("candidate_selector", "body_complete_marker")
        if getattr(profile, name) is None
    )
    missing.extend(f"fields.{name}" for name in ("body", "author") if name not in profile.fields)
    if "title" not in profile.fields and profile.title_not_displayed_marker is None:
        missing.append("fields.title or title_not_displayed_marker")
    if "filter_entry" in profile.actions:
        if "filter" not in profile.pages:
            missing.append("pages.filter")
        missing.extend(
            f"actions.{name}" for name in ("image_text_filter", "filter_confirm")
            if name not in profile.actions
        )
        if profile.image_text_selected_marker is None:
            missing.append("image_text_selected_marker")
    return missing


def _metadata_checks(profile: Profile, snapshot: Snapshot | None) -> dict:
    result = {}
    metadata = snapshot.metadata if snapshot else {}
    expected_values = {
        "app_package": profile.app_package,
        "app_version": profile.app_version,
        "resolution": profile.reference_resolution,
    }
    for name, expected in expected_values.items():
        configured = min(expected) > 0 if name == "resolution" else bool(expected.strip())
        if not configured:
            result[name] = {"status": "not_checked", "reason": "not_configured"}
            continue
        if snapshot is None:
            result[name] = {"status": "not_checked", "reason": "sample_not_provided"}
            continue
        if name == "app_package":
            actual = metadata.get("package", metadata.get("app_package"))
            if actual != expected:
                raise ProfileError("Draft App package does not match the sample foreground package")
            for key in ("configured_package", "app_package"):
                if metadata.get(key) not in (None, "", expected):
                    raise ProfileError(f"Draft App package conflicts with sample {key}")
        elif name == "resolution":
            actual = metadata.get("resolution")
            if not isinstance(actual, (tuple, list)) or tuple(actual) != expected:
                raise ProfileError("Draft resolution does not match sample resolution")
        elif metadata.get(name) != expected:
            raise ProfileError("Draft App version does not match sample App version")
        result[name] = {"status": "match"}
    return result


def inspect_draft(profile: Profile, snapshot: Snapshot | None = None) -> dict:
    """Count visible selector matches; never invoke production parsing or any device API."""
    report = {
        "ok": True,
        "mode": "draft",
        "profile": profile.name,
        "verified": profile.verified,
        "profile_synthetic": profile.synthetic,
        "production_validated": False,
        "sample_checked": snapshot is not None,
        "missing_production_requirements": missing_production_requirements(profile),
        "metadata_checks": _metadata_checks(profile, snapshot),
        "note": "仅检查草稿结构及已配置规则的命中；不修改verified，不验证生产可用性。",
    }
    if snapshot is None:
        return report
    root = xml_root(snapshot)
    report["sample_synthetic"] = (
        snapshot.metadata.get("source_kind") == "synthetic"
        or snapshot.metadata.get("synthetic") is True
        or root.get("synthetic", "").lower() == "true"
    )
    report["synthetic"] = profile.synthetic or report["sample_synthetic"]
    report["page_rule_matches"] = {}
    for name, rule in profile.pages.items():
        all_counts = [len(matching(root, selector)) for selector in rule.all]
        any_counts = [len(matching(root, selector)) for selector in rule.any]
        none_counts = [len(matching(root, selector)) for selector in rule.none]
        activity_match = (
            snapshot.metadata.get("activity") == rule.activity
            if rule.activity is not None else None
        )
        report["page_rule_matches"][name] = {
            "all_counts": all_counts,
            "any_counts": any_counts,
            "none_counts": none_counts,
            "activity_match": activity_match,
            "matched": (all(all_counts) and (not any_counts or any(any_counts))
                        and not any(none_counts) and activity_match is not False),
        }
    report["action_matches"] = {}
    resolution_checked = report["metadata_checks"]["resolution"]["status"] == "match"
    for name, selector in profile.actions.items():
        nodes = matching(root, selector)
        bounds = (
            bounds_for(nodes[0], profile.reference_resolution)
            if len(nodes) == 1 and resolution_checked else None
        )
        report["action_matches"][name] = {"match_count": len(nodes), "unique_bounds": bounds}
    report["selector_matches"] = {
        name: len(matching(root, selector))
        for name in (
            "candidate_selector", "candidate_tap_selector", "candidate_video_marker",
            "body_complete_marker",
            "title_not_displayed_marker", "image_text_selected_marker",
        )
        if (selector := getattr(profile, name)) is not None
    }
    return report
