"""Optional local OCR limited to explicitly calibrated visible regions."""

import csv
import io
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from xhs_mobile.domain import FieldValue, Snapshot
from xhs_mobile.profile import Bounds


class LocalOCR:
    def __init__(self, executable: str = "tesseract", timeout: float = 15,
                 min_confidence: float = 0.8):
        if timeout <= 0 or not 0 <= min_confidence <= 1:
            raise ValueError("OCR needs a positive timeout and confidence between 0 and 1")
        self.executable = executable
        self.timeout = timeout
        self.min_confidence = min_confidence

    def read(self, snapshot: Snapshot, region: Bounds) -> FieldValue:
        result = FieldValue(method="ocr", region=list(region))
        try:
            with Image.open(io.BytesIO(snapshot.png)) as image:
                x1, y1, x2, y2 = region
                if not (0 <= x1 < x2 <= image.width and 0 <= y1 < y2 <= image.height):
                    result.reason = "ocr_region_out_of_bounds"
                    return result
                cropped = image.crop(region)
                with tempfile.TemporaryDirectory(prefix="xhs-mobile-ocr-") as directory:
                    path = Path(directory) / "visible-region.png"
                    cropped.save(path)
                    process = subprocess.run(
                        [self.executable, str(path), "stdout", "-l", "chi_sim+eng",
                         "--psm", "6", "tsv"],
                        capture_output=True, text=True, timeout=self.timeout, check=True,
                    )
        except FileNotFoundError:
            result.reason = "tesseract_not_installed"
            return result
        except subprocess.TimeoutExpired:
            result.reason = "ocr_timeout"
            return result
        except subprocess.CalledProcessError:
            result.reason = "tesseract_failed_check_local_language_packs"
            return result
        except (OSError, UnidentifiedImageError, ValueError):
            result.reason = "ocr_invalid_image"
            return result

        lines: dict[tuple[str, ...], list[str]] = {}
        weighted_confidence = 0.0
        characters = 0
        try:
            for row in csv.DictReader(io.StringIO(process.stdout), delimiter="\t"):
                text = (row.get("text") or "").strip()
                confidence = float(row.get("conf", "-1"))
                if not text or confidence < 0:
                    continue
                if not math.isfinite(confidence) or confidence > 100:
                    raise ValueError("invalid OCR confidence")
                key = tuple(
                    row.get(k, "") for k in ("page_num", "block_num", "par_num", "line_num")
                )
                lines.setdefault(key, []).append(text)
                weighted_confidence += confidence * len(text)
                characters += len(text)
        except (TypeError, ValueError):
            result.reason = "ocr_invalid_tsv"
            return result
        if not characters:
            result.reason = "ocr_no_text"
            return result
        result.raw = "\n".join(" ".join(words) for words in lines.values())
        result.confidence = weighted_confidence / characters / 100
        result.status = "present" if result.confidence >= self.min_confidence else "low_quality"
        result.reason = (
            "ocr_visible_region_only" if result.status == "present" else "ocr_low_confidence"
        )
        return result
