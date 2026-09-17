"""Identity from a calibrated Android copy-link action, never text similarity.

Short-link resolution inspects only HTTP redirects. It does not fetch note
content, send messages, use browser cookies or inspect the Mac clipboard.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .domain import DeviceError
from .profile import classify, locate, matching, xml_root

_NOTE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}
_SHORT_HOSTS = {"xhslink.cn", "xhslink.com"}


def canonical_identity(url: str) -> tuple[str, str] | None:
    """Only standard HTTPS note links with a full hexadecimal note identity."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _NOTE_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?", parsed.path)
    if not match:
        return None
    note_id = match.group(1).lower()
    return note_id, f"https://www.xiaohongshu.com/explore/{note_id}"


def _allowed(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        return (p.scheme == "https" and p.hostname in _SHORT_HOSTS | _NOTE_HOSTS
                and not p.username and not p.password and p.port in (None, 443)
                and len(url) <= 4096 and not any(ord(c) < 33 for c in url))
    except ValueError:
        return False


def _copied_urls(text: str) -> list[str]:
    urls = [u.rstrip(".,，。)）") for u in re.findall(r"https://[^\s<>\"']+", text)]
    return [url for url in urls if _allowed(url)]


def _source_url(url: str) -> str:
    """Auditable public host/path only; never persist volatile query tokens."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _redirect(url: str, timeout: float) -> str | None:
    # No cookies, credentials or user-supplied proxy authentication are sent.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout):
            return None
    except urllib.error.HTTPError as exc:
        try:
            if exc.code in (301, 302, 303, 307, 308):
                return exc.headers.get("Location")
        finally:
            exc.close()
    return None


def resolve_copied_link(
    text: str, *, timeout: float = 12, cancelled=lambda: False,
    redirect=_redirect, monotonic=time.monotonic, resolution_trace: list[dict] | None = None,
) -> tuple[str, str] | None:
    """Bounded optional resolution; no short token or body text becomes an ID."""
    candidates = _copied_urls(text)
    if len(candidates) != 1:
        return None
    current = candidates[0]
    deadline = monotonic() + timeout
    visited = set()
    for _ in range(4):
        if cancelled() or current in visited or not _allowed(current):
            return None
        identity = canonical_identity(current)
        if identity:
            return identity
        visited.add(current)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        try:
            destination = redirect(current, min(remaining, 4))
        except (OSError, ValueError):
            return None
        if not destination:
            return None
        destination = urllib.parse.urljoin(current, destination)
        if not _allowed(destination):
            return None
        if resolution_trace is not None:
            resolution_trace.append({"from_url": _source_url(current),
                                     "to_url": _source_url(destination)})
        current = destination
    return None


def _visit_anchor(state, profile) -> dict[str, str]:
    """Only a continuity check within one share round-trip, not dedup identity."""
    result = {}
    root = xml_root(state)
    for name in ("author", "title", "body"):
        rule = profile.fields.get(name)
        if rule is None:
            continue
        nodes = matching(root, rule.selector)
        if len(nodes) == 1:
            raw = nodes[0].get(rule.attribute, "")
            if raw:
                result[name] = raw
    return result


def read_note_identity(runner: Any, initial_state) -> dict | None:
    """One calibrated share/copy/return round-trip with fresh Android clipboard.

    Policy/pause/read-threshold exceptions propagate. An unsupported clipboard
    or unresolvable link simply leaves identity unverified. Never retry an
    uncertain tap, and never silently navigate past an unknown share page.
    """
    profile = runner.profile
    if (not {"share_entry", "copy_link", "share_close"} <= profile.actions.keys()
            or "share" not in profile.pages
            or not callable(getattr(runner.device, "set_clipboard", None))
            or not callable(getattr(runner.device, "get_clipboard", None))):
        return None
    if classify(initial_state, profile) != "detail":
        return None
    before = _visit_anchor(initial_state, profile)
    if not before.get("author") or locate(initial_state, profile, "share_entry") is None:
        return None
    marker = "xhs-mobile-copy-" + uuid.uuid4().hex
    try:
        runner.device.set_clipboard(marker)
        if runner.device.get_clipboard() != marker:
            return None
    except DeviceError:
        return None
    runner._pause_check()
    # Reobserve after clipboard calls; never use their preceding coordinates.
    detail = runner._wait({"detail"}, "identity_before_share")
    if _visit_anchor(detail, profile) != before:
        return None
    runner._click_action(detail, "share_entry")
    share = runner._wait({"share"}, "identity_share")
    runner._click_action(share, "copy_link")
    returned = runner._wait({"detail", "share"}, "identity_copy_return")
    if classify(returned, profile) == "share":
        runner._click_action(returned, "share_close")
        returned = runner._wait({"detail"}, "identity_close_share")
    if _visit_anchor(returned, profile) != before:
        return None
    try:
        copied = runner.device.get_clipboard()
    except DeviceError:
        return None
    if copied == marker:
        return None
    runner._pause_check()
    resolution_trace: list[dict] = []
    identity = resolve_copied_link(
        copied, cancelled=runner.stop_requested, resolution_trace=resolution_trace,
    )
    runner._pause_check()
    if identity is None:
        return None
    note_id, canonical_url = identity
    copied_source_url = _source_url(_copied_urls(copied)[0])
    copied_short_url = (copied_source_url
                        if urllib.parse.urlsplit(copied_source_url).hostname in _SHORT_HOSTS
                        else None)
    proof = {
        "method": "android_share_copy_link", "note_id": note_id,
        "canonical_url": canonical_url,
        "copied_source_url": copied_source_url,
        "copied_short_url": copied_short_url,
        "source_url_redactions": ["query", "fragment"],
        "resolution_method": "validated_http_redirect" if resolution_trace else "standard_note_url",
        "resolution_trace": resolution_trace,
        "copied_text_sha256": hashlib.sha256(copied.encode()).hexdigest(),
        "freshness_marker_sha256": hashlib.sha256(marker.encode()).hexdigest(),
        "before_ui_sha256": hashlib.sha256(detail.xml.encode()).hexdigest(),
        "returned_ui_sha256": hashlib.sha256(returned.xml.encode()).hexdigest(),
        "captured_at": returned.captured_at.isoformat(),
        "calibration_evidence": profile.calibration_evidence,
    }
    returned.metadata["identity_proof"] = proof
    return {"note_id": note_id, "canonical_url": canonical_url,
            "identity_source": "android_share_copy_link", "identity_proof": proof,
            "evidence": [], "state": returned}
