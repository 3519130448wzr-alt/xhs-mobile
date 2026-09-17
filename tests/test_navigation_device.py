"""Synthetic driver responses; these tests never contact Android."""

from unittest.mock import Mock

import pytest

from xhs_mobile.device import AndroidDevice
from xhs_mobile.domain import DeviceUnavailable, UIState


def test_navigation_is_not_evidence_and_does_not_request_png(monkeypatch):
    phone = AndroidDevice("synthetic-phone", "org.synthetic.notes")
    worker = Mock(return_value={"xml": "<hierarchy/>", "resolution": [720, 1280]})
    shell = Mock(return_value="versionName=synthetic-v1")
    monkeypatch.setattr(phone, "_u2", worker)
    monkeypatch.setattr(phone, "_shell", shell)
    monkeypatch.setattr(phone, "_foreground", lambda: {
        "package": "org.synthetic.notes", "activity": "SyntheticActivity"})
    for _ in range(2):
        state = phone.read_state()
        assert isinstance(state, UIState)
        assert not hasattr(state, "png")
        assert state.metadata["capture_timings"]["screenshot_transfer_seconds"] == 0
        assert state.metadata["app_version"] == "synthetic-v1"
    assert shell.call_count == 1  # stable version is cached within this driver
    assert all(call.args == ("navigation_state",) for call in worker.call_args_list)


@pytest.mark.parametrize("result", [None, "", {"xml": "", "resolution": [1, 2]},
                                      {"xml": "<hierarchy/>", "resolution": [720, False]}])
def test_navigation_invalid_worker_data_fails_explicitly(monkeypatch, result):
    phone = AndroidDevice("synthetic-phone", "org.synthetic.notes")
    monkeypatch.setattr(phone, "_u2", Mock(return_value=result))
    with pytest.raises(DeviceUnavailable):
        phone.read_state()


def test_clipboard_is_bound_to_android_worker(monkeypatch):
    phone = AndroidDevice("synthetic-phone", "org.synthetic.notes")
    worker = Mock(side_effect=[None, "synthetic fresh link"])
    monkeypatch.setattr(phone, "_u2", worker)
    phone.set_clipboard("synthetic sentinel")
    assert phone.get_clipboard() == "synthetic fresh link"
    assert worker.call_args_list[0].kwargs == {"text": "synthetic sentinel"}
    assert worker.call_args_list[1].args == ("get_clipboard",)
