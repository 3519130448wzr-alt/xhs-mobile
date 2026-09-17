"""Small shared contracts; drivers and collectors do not depend on storage."""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class UIState:
    """Lightweight navigation read; deliberately cannot be saved as image evidence."""

    xml: str
    captured_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    xml: str
    png: bytes
    captured_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldValue:
    raw: str | None = None
    status: str = "not_readable"
    method: str = "none"
    reason: str | None = None
    normalized: int | None = None
    approximate: bool = False
    confidence: float | None = None
    region: list[int] | None = None
    evidence_ref: str | None = None


def base_readability(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Readability and identity/manual completeness are independent assessments."""
    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        return False, ["title", "author", "body"]
    missing = []
    for name in ("title", "author", "body"):
        value = fields.get(name, {})
        if not isinstance(value, dict):
            missing.append(name)
            continue
        if name == "title" and value.get("status") == "not_displayed":
            continue
        if (value.get("status") != "present" or not isinstance(value.get("raw"), str)
                or not value["raw"].strip()):
            missing.append(name)
    if data.get("content_type", "image_text") != "image_text":
        missing.append("content_type")
    return not missing, missing


@dataclass
class ParsedNote:
    fields: dict[str, FieldValue]
    note_id: str | None = None
    canonical_url: str | None = None
    identity_source: str | None = None
    content_type: str = "image_text"
    body_complete: bool | None = None
    completeness_status: str = "not_assessed"
    quality_revision: int = 2
    time_kind: str = "not_readable"
    topics_status: str = "unrecognized"
    topics: list[str] = field(default_factory=list)
    topic_sources: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return base_readability(asdict(self))[0]

    @property
    def identity_status(self) -> str:
        return "verified" if self.note_id else "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "identity_status": self.identity_status}


@dataclass(frozen=True)
class Candidate:
    """Session-local location. Never persist as an address or an identity."""

    key: str
    bounds: tuple[int, int, int, int]


class DeviceError(RuntimeError):
    pass


class DeviceUnavailable(DeviceError):
    pass


class DeviceBusy(DeviceError):
    pass


class PageError(RuntimeError):
    pass


class UnknownPage(PageError):
    pass


class ProfileError(PageError):
    pass


class EvidenceError(RuntimeError):
    pass


class Device(Protocol):
    serial: str

    def health(self) -> dict[str, Any]: ...
    def capture(self) -> Snapshot: ...
    def read_state(self) -> UIState: ...
    def start_app(self, package: str) -> None: ...
    def stop_app(self, package: str) -> None: ...
    def click(self, bounds: tuple[int, int, int, int]) -> None: ...
    def input_text(self, text: str) -> None: ...
    def press(self, key: str) -> None: ...
    def swipe(self, direction: str = "up") -> None: ...


class EvidenceSink(Protocol):
    def save(self, snapshot: Snapshot, *, device_id: str, run_id: str, label: str) -> dict: ...
