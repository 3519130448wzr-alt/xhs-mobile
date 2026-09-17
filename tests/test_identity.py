"""Synthetic copied links and share flow, with no phone or network access."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xhs_mobile import identity
from xhs_mobile.domain import UIState

NOTE_ID = "abcdef0123456789abcdef01"
STANDARD = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


@pytest.mark.parametrize("url", [
    "https://xhslink.cn/o/short-token", "http://www.xiaohongshu.com/explore/" + NOTE_ID,
    "https://www.xiaohongshu.com.evil.example/explore/" + NOTE_ID,
    "https://user:secret@www.xiaohongshu.com/explore/" + NOTE_ID,
    "https://www.xiaohongshu.com:5555/explore/" + NOTE_ID,
    "https://www.xiaohongshu.com/explore/short-token",
])
def test_nonstandard_links_are_not_identity(url):
    assert identity.canonical_identity(url) is None


def test_standard_link_strips_tracking_and_tokens():
    assert identity.canonical_identity(STANDARD + "?xsec_token=synthetic") == (NOTE_ID, STANDARD)


def test_short_link_requires_actual_validated_redirect():
    redirect = Mock(return_value=f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}")
    resolved = identity.resolve_copied_link(
        "合成分享 https://xhslink.cn/o/token", redirect=redirect,
    )
    assert resolved == (NOTE_ID, STANDARD)
    assert redirect.call_count == 1


@pytest.mark.parametrize("destination", ["https://127.0.0.1/private", "https://other.example/",
                                          "https://xhslink.cn/o/token", None])
def test_optional_redirect_failure_does_not_follow_arbitrary_hosts(destination):
    redirect = Mock(return_value=destination)
    assert identity.resolve_copied_link("https://xhslink.cn/o/token", redirect=redirect) is None
    assert redirect.call_count == 1


def test_ambiguous_copied_links_or_cancelled_read_not_resolved():
    redirect = Mock(side_effect=AssertionError("Must not contact network"))
    assert identity.resolve_copied_link(STANDARD + " " + STANDARD, redirect=redirect) is None
    assert identity.resolve_copied_link(STANDARD, cancelled=lambda: True, redirect=redirect) is None


def synthetic_runner(monkeypatch, *, stale=False, switched=False):
    states = [UIState("<synthetic/>", metadata={"page": page})
              for page in ("detail", "share", "detail")]
    actions = {k: None for k in ("share_entry", "copy_link", "share_close")}
    profile = SimpleNamespace(actions=actions,
                              pages={"share": None}, calibration_evidence=["synthetic-only"])
    phone = SimpleNamespace(clipboard="old synthetic clipboard")
    phone.set_clipboard = lambda text: setattr(phone, "clipboard", text)
    phone.get_clipboard = lambda: STANDARD if stale else phone.clipboard
    clicks = []

    def click(state, action):
        clicks.append(action)
        if action == "copy_link":
            phone.clipboard = STANDARD

    runner = SimpleNamespace(profile=profile, device=phone, _pause_check=lambda: None,
                             _wait=Mock(side_effect=states), _click_action=click,
                             stop_requested=lambda: False)
    monkeypatch.setattr(identity, "classify", lambda state, profile: state.metadata["page"])
    monkeypatch.setattr(identity, "locate", lambda *args: (1, 2, 3, 4))
    monkeypatch.setattr(identity, "_visit_anchor", lambda state, profile: {
        "author": "switched" if switched and state is states[-1] else "synthetic author"})
    return runner, clicks, UIState("<synthetic/>", metadata={"page": "detail"})


def test_fresh_copy_roundtrip_returns_traceable_identity(monkeypatch):
    runner, clicks, initial = synthetic_runner(monkeypatch)
    result = identity.read_note_identity(runner, initial)
    assert result["note_id"] == NOTE_ID
    assert result["identity_proof"]["freshness_marker_sha256"]
    assert clicks == ["share_entry", "copy_link"]


def test_stale_clipboard_cannot_be_attached_to_current_note(monkeypatch):
    runner, clicks, initial = synthetic_runner(monkeypatch, stale=True)
    assert identity.read_note_identity(runner, initial) is None
    assert clicks == []


def test_changed_detail_after_copy_drops_identity(monkeypatch):
    runner, _, initial = synthetic_runner(monkeypatch, switched=True)
    assert identity.read_note_identity(runner, initial) is None


def test_redirect_proof_retains_public_source_without_tracking_tokens():
    trace = []
    source = "https://xhslink.cn/o/public-token?xsec_token=SYNTHETIC_SECRET#private"
    destination = STANDARD + "?xsec_token=SYNTHETIC_OTHER_SECRET&tracking=value#private"
    assert identity.resolve_copied_link(
        source, redirect=Mock(return_value=destination), resolution_trace=trace,
    ) == (NOTE_ID, STANDARD)
    assert trace == [{"from_url": "https://xhslink.cn/o/public-token", "to_url": STANDARD}]
    assert "SECRET" not in str(trace)


def test_standard_copy_proof_retains_sanitized_source_link(monkeypatch):
    runner, _, initial = synthetic_runner(monkeypatch)
    original_click = runner._click_action

    def copied_with_token(state, action):
        original_click(state, action)
        if action == "copy_link":
            runner.device.clipboard = STANDARD + "?xsec_token=SYNTHETIC_SECRET#fragment"

    runner._click_action = copied_with_token
    result = identity.read_note_identity(runner, initial)
    proof = result["identity_proof"]
    assert proof["copied_source_url"] == STANDARD
    assert proof["copied_short_url"] is None
    assert proof["resolution_method"] == "standard_note_url"
    assert proof["resolution_trace"] == []
    assert "SYNTHETIC_SECRET" not in str(proof)
