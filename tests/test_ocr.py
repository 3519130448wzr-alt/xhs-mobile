import io
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from xhs_mobile.domain import Snapshot
from xhs_mobile.ocr import LocalOCR


def snapshot():
    # SYNTHETIC blank pixels; never real collection evidence.
    stream = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(stream, format="PNG")
    return Snapshot(xml="<SYNTHETIC/>", png=stream.getvalue())


def tsv(confidence):
    return ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tconf\ttext\n"
            f"5\t1\t1\t1\t1\t1\t{confidence}\tSYNTHETIC\n")


@pytest.mark.parametrize("confidence,status", [(98, "present"), (20, "low_quality")])
def test_local_ocr_crop_language_timeout_and_quality(monkeypatch, confidence, status):
    def run(args, **kwargs):
        assert args[0] == "tesseract"
        assert args[args.index("-l") + 1] == "chi_sim+eng"
        assert args[-1] == "tsv"
        assert kwargs["timeout"] == 7
        with Image.open(args[1]) as cropped:
            assert cropped.size == (40, 50)
        return SimpleNamespace(stdout=tsv(confidence))

    monkeypatch.setattr(subprocess, "run", run)
    result = LocalOCR(timeout=7).read(snapshot(), (10, 10, 50, 60))
    assert result.raw == "SYNTHETIC"
    assert result.status == status
    assert result.confidence == confidence / 100
    assert result.region == [10, 10, 50, 60]


def test_out_of_bounds_never_runs_ocr(monkeypatch):
    def run(*args, **kwargs):
        pytest.fail("OCR must not run for an invalid region")

    monkeypatch.setattr(subprocess, "run", run)
    assert LocalOCR().read(snapshot(), (0, 0, 101, 100)).reason == "ocr_region_out_of_bounds"


@pytest.mark.parametrize("error,reason", [
    (FileNotFoundError(), "tesseract_not_installed"),
    (subprocess.TimeoutExpired("tesseract", 15), "ocr_timeout"),
    (subprocess.CalledProcessError(1, "tesseract"), "tesseract_failed_check_local_language_packs"),
])
def test_ocr_errors_keep_field_missing(monkeypatch, error, reason):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    result = LocalOCR().read(snapshot(), (0, 0, 10, 10))
    assert result.raw is None
    assert result.reason == reason
    assert result.status == "not_readable"
