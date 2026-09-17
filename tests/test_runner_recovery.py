"""Behavioral recovery tests using explicitly SYNTHETIC Android screens.

No device connection, genuine App selector, or real collection evidence is used.
Each test owns a SQLite database and private local evidence directory. PostgreSQL
is covered separately by the project's explicit integration-test opt-in.
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import create_engine, select

from xhs_mobile.config import Policy
from xhs_mobile.connection import ConnectionFailure
from xhs_mobile.domain import DeviceError, DeviceUnavailable, EvidenceError, Snapshot, UIState
from xhs_mobile.evidence import EvidenceStore
from xhs_mobile.models import Base, Event, Run
from xhs_mobile.profile import PageRule, Selector, load_profile
from xhs_mobile.repository import Repository, aware
from xhs_mobile.runner import Runner

FIXTURES = Path(__file__).parent / "fixtures"
ENTRY = (10, 10, 110, 70)
INPUT = (10, 90, 700, 150)
SUBMIT = (800, 90, 1000, 150)
CARD = (20, 200, 1000, 700)
FILTER_ENTRY = (10, 160, 110, 190)
FILTER_CHOICE = (120, 160, 240, 190)
FILTER_CONFIRM = (800, 160, 1000, 190)
DEFAULT_NOTE_ID = "0123456789abcdef01234567"


def card_bounds(index):
    assert index in (0, 1), "Synthetic phone supports at most two cards per viewport"
    return (20, 200 + index * 600, 1000, 700 + index * 600)


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.origin = datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        assert seconds >= 0
        self.elapsed += seconds
        assert self.elapsed < 10_000, "Synthetic clock detected an unbounded recovery loop"

    def now(self):
        return self.origin + timedelta(seconds=self.elapsed)


def node(resource_id, bounds=None, text=""):
    location = ""
    if bounds is not None:
        x1, y1, x2, y2 = bounds
        location = f' bounds="[{x1},{y1}][{x2},{y2}]"'
    return f'<node resource-id="synthetic/{resource_id}" text="{escape(text)}"{location}/>'


class SyntheticPhone:
    """Minimal UI state machine; recovery is driven by the real Runner."""

    serial = "SYNTHETIC-recovery-device"
    package = "test.synthetic.app"

    def __init__(self, clock, cooldown=10):
        self.clock = clock
        self.cooldown = cooldown
        self.page = "home"
        self.keyword = ""
        self.writes = []
        self.capture_count = 0
        self.read_count = 0
        self._state_only = False
        self.disconnect_captures = 0
        self.crash_on_candidate = False
        self.crashes = 0
        self.fail_launch = False
        self.launch_failures = 0
        self.limit_on_candidate = False
        self.limit_persists = False
        self.limit_started_at = None
        self.rate_captures = []
        self.search_submissions = 0
        self.result_pages = [[DEFAULT_NOTE_ID]]
        self.result_page_index = 0
        self.current_note_id = DEFAULT_NOTE_ID
        self.opened_note_ids = []

    def _write(self, name, *args):
        self.writes.append((self.clock.elapsed, name, args))

    def health(self):
        return {"ok": True, "source_kind": "synthetic"}

    def read_state(self):
        self.read_count += 1
        self._state_only = True
        try:
            snap = self.capture()
        finally:
            self._state_only = False
        return UIState(xml=snap.xml, captured_at=snap.captured_at, metadata=snap.metadata)

    def capture(self):
        if not self._state_only:
            self.capture_count += 1
        if self.disconnect_captures:
            self.disconnect_captures -= 1
            raise DeviceUnavailable("SYNTHETIC temporary ADB disconnect")
        if (
            self.page == "rate_limited"
            and not self.limit_persists
            and self.clock.elapsed >= self.limit_started_at + self.cooldown
        ):
            self.page = "detail"
        if self.page == "rate_limited" and not self._state_only:
            self.rate_captures.append(self.clock.elapsed)
        if self.page == "detail":
            xml = (FIXTURES / "synthetic_detail.xml").read_text(encoding="utf-8")
            xml = xml.replace(DEFAULT_NOTE_ID, self.current_note_id)
        elif self.page == "home":
            xml = node("home") + node("search-entry", ENTRY)
        elif self.page == "search":
            xml = node("search") + node("search-input", INPUT, self.keyword)
            xml += node("search-submit", SUBMIT)
        elif self.page == "results":
            xml = node("results") + node("search-input", INPUT, self.keyword)
            xml += "".join(
                node("card", card_bounds(i), f"SYNTHETIC candidate {note_id}")
                for i, note_id in enumerate(self.result_pages[self.result_page_index])
            )
        elif self.page == "rate_limited":
            xml = node("rate-limited")
        elif self.page in {"login", "verification", "restricted"}:
            xml = node(self.page)
        else:
            xml = node("unrecognized-screen")
        if not xml.startswith("<?xml"):
            xml = '<hierarchy synthetic="true">' + xml + "</hierarchy>"
        package = "test.synthetic.launcher" if self.page == "launcher" else self.package
        return Snapshot(
            xml=xml,
            png=b"SYNTHETIC offline screenshot bytes; never real-device evidence",
            captured_at=self.clock.now(),
            metadata={
                "source_kind": "synthetic", "app_version": "SYNTHETIC-1",
                "configured_package": self.package, "package": package,
                "app_package": package, "resolution": [1080, 1920],
            },
        )

    def start_app(self, package):
        assert package == self.package
        self._write("start_app", package)
        if self.fail_launch:
            self.launch_failures += 1
            raise DeviceUnavailable("SYNTHETIC persistent launch disconnect")
        if self.page == "launcher":
            self.page = "home"

    def stop_app(self, package):
        assert package == self.package
        self._write("stop_app", package)
        self.page = "launcher"

    def click(self, bounds):
        bounds = tuple(bounds)
        self._write("click", bounds)
        if bounds == ENTRY:
            self.page = "search"
        elif bounds == INPUT:
            self.page = "search"
        elif bounds == SUBMIT:
            self.search_submissions += 1
            self.page = "results"
            self.result_page_index = 0
        elif bounds in {card_bounds(i) for i in (0, 1)}:
            position = 0 if bounds == CARD else 1
            self.current_note_id = self.result_pages[self.result_page_index][position]
            self.opened_note_ids.append(self.current_note_id)
            if self.crash_on_candidate:
                self.crash_on_candidate = False
                self.crashes += 1
                self.page = "launcher"
            elif self.limit_on_candidate:
                self.limit_on_candidate = False
                self.page = "rate_limited"
                self.limit_started_at = self.clock.elapsed
            else:
                self.page = "detail"
        else:
            raise AssertionError(f"Synthetic test has no calibrated action at {bounds}")

    def input_text(self, text):
        self._write("input_text", text)
        assert self.page == "search"
        self.keyword = text

    def press(self, key):
        self._write("press", key)
        assert key == "back"
        self.page = "results" if self.page == "detail" else "home"

    def swipe(self, direction="up"):
        self._write("swipe", direction)
        assert self.page == "results"
        self.result_page_index = min(self.result_page_index + 1, len(self.result_pages) - 1)


@pytest.fixture
def recovery(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic-recovery.sqlite'}")
    Base.metadata.create_all(engine)
    repo = Repository(engine)
    clock = Clock()
    profile = load_profile(FIXTURES / "synthetic_profile.toml", require_verified=False)
    profile.actions.pop("image_text_filter")
    profile.actions.pop("back_to_results")
    policy = Policy(action_interval=0.1, page_timeout=1, cooldown_seconds=10)
    phone = SyntheticPhone(clock, cooldown=policy.cooldown_seconds)
    evidence = EvidenceStore(tmp_path / "synthetic-evidence")

    def create_task(target=1):
        return repo.create_task(
            device_id="synthetic-device", serial=phone.serial,
            session_ref="synthetic-session", keyword="SYNTHETIC 中文关键词",
            target=target, policy=policy.model_dump(),
        )

    def runner(sink=None, stop_requested=lambda: False, connection=None):
        return Runner(
            repository=repo, device=phone, evidence=sink or evidence,
            profile=profile, sleep=clock.sleep,
            monotonic=clock.monotonic, now=clock.now,
            stop_requested=stop_requested,
            connection=connection, identity_reader=lambda *_: None,
        )

    yield repo, phone, clock, policy, evidence, create_task, runner
    engine.dispose()


def assert_collected(repo, task, count=1):
    status = repo.status(task.id)["tasks"][0]
    assert status["status"] == "collected_awaiting_review", status
    assert status["eligible_count"] == count
    assert status["observation_count"] == count


def synthetic_body_frame(*, missing=(), complete=True, body="SYNTHETIC visible body"):
    root = ET.fromstring((FIXTURES / "synthetic_detail.xml").read_text(encoding="utf-8"))
    detail = root.find("node")
    absent = set(missing) | ({"body-end"} if not complete else set())
    for field in list(detail):
        if field.get("resource-id").removeprefix("synthetic/") in absent:
            detail.remove(field)
        elif field.get("resource-id") == "synthetic/body":
            field.set("text", body)
    ET.SubElement(detail, "node", {
        "resource-id": "synthetic/comment-body", "text": "SYNTHETIC ignored comment text"
    })
    return ET.tostring(root, encoding="unicode")


@pytest.fixture
def body_scroll(recovery, monkeypatch):
    repo, phone, clock, policy, evidence, _, runner = recovery
    phone.page = "detail"
    phone.detail_frames = [synthetic_body_frame()]
    phone.detail_position = 0
    phone.detail_scrolls = 0
    capture, swipe = phone.capture, phone.swipe

    def frame_capture():
        snap = capture()
        if phone.page == "detail":
            snap.xml = phone.detail_frames[min(phone.detail_position, len(phone.detail_frames) - 1)]
        return snap

    def detail_swipe(direction="up"):
        if phone.page != "detail":
            return swipe(direction)
        phone._write("swipe_detail", direction)
        phone.detail_scrolls += 1
        phone.detail_position += 1

    monkeypatch.setattr(phone, "capture", frame_capture)
    monkeypatch.setattr(phone, "swipe", detail_swipe)
    profile = runner().profile

    def create_current():
        return repo.create_task(
            device_id="synthetic-device", serial=phone.serial,
            session_ref="synthetic-session", keyword="SYNTHETIC current detail",
            target=1, policy=policy.model_dump(), mode="current",
        )

    return repo, phone, clock, policy, evidence, profile, create_current, runner


def test_detail_scroll_reads_complete_body_from_one_saved_frame(body_scroll):
    repo, phone, _, _, evidence, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    profile.fields.pop("published_at")
    phone.detail_frames = [
        synthetic_body_frame(missing=("title", "body"), complete=False),
        synthetic_body_frame(body="SYNTHETIC complete body after one scroll"),
        synthetic_body_frame(body="SYNTHETIC this later frame must never be reached"),
    ]
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.detail_scrolls == 1
    assert repo.task(task.id).retry_counts["body_swipes"] == 1
    row = repo.observations(task.id)[0]
    assert row["data"]["fields"]["body"]["raw"] == "SYNTHETIC complete body after one scroll"
    assert "comment-body" not in row["data"]["fields"]
    assert [m["label"] for m in row["evidence"]] == ["detail_initial", "detail_body_scroll_1"]
    assert row["captured_at"] == row["evidence"][1]["captured_at"]
    assert all(evidence.verify(manifest) for manifest in row["evidence"])
    with repo.sessions() as session:
        saved = session.scalar(select(Event).where(Event.kind == "observation_saved"))
    assert saved.detail["selected_evidence_id"] == row["evidence"][1]["evidence_id"]


@pytest.mark.parametrize("max_swipes", [0, 2])
def test_detail_with_no_new_body_is_bounded_and_stays_incomplete(body_scroll, max_swipes):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = max_swipes
    phone.detail_frames = [synthetic_body_frame(missing=("body",), complete=False)]
    task = create_task()
    runner().execute(task.id)
    assert phone.detail_scrolls == max_swipes
    assert repo.task(task.id).retry_counts.get("body_swipes", 0) == max_swipes
    assert repo.task(task.id).status == "partial"
    row = repo.observations(task.id)[0]
    assert not row["eligible"]
    assert not row["data"]["body_complete"]
    assert row["data"]["fields"]["body"]["raw"] is None


def test_worse_later_detail_frame_does_not_replace_better_initial_frame(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 2
    phone.detail_frames = [
        synthetic_body_frame(complete=False, body="SYNTHETIC earlier incomplete body"),
        synthetic_body_frame(missing=("title", "author"), complete=False, body="SYNTHETIC later"),
    ]
    task = create_task()
    runner().execute(task.id)
    row = repo.observations(task.id)[0]
    assert phone.detail_scrolls == 2, "Missing-time probes stay within the configured swipe bound"
    assert row["data"]["fields"]["body"]["raw"] == "SYNTHETIC earlier incomplete body"
    assert row["eligible"]
    assert not row["data"]["body_complete"]
    assert row["captured_at"] == row["evidence"][0]["captured_at"]
    with repo.sessions() as session:
        saved = session.scalar(select(Event).where(Event.kind == "observation_saved"))
    assert saved.detail["selected_evidence_id"] == row["evidence"][0]["evidence_id"]


def test_complementary_detail_frames_are_not_merged_into_eligible_record(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    phone.detail_frames = [
        synthetic_body_frame(missing=("body",), complete=False),
        synthetic_body_frame(missing=("author",), complete=True),
    ]
    task = create_task()
    runner().execute(task.id)
    assert phone.detail_scrolls == 3, "Readability, not a completeness marker, bounds recovery"
    row = repo.observations(task.id)[0]
    assert not row["eligible"]
    assert row["data"]["body_complete"] is None
    assert row["data"]["fields"]["author"]["raw"] is None


def test_body_swipe_total_budget_survives_new_runs(body_scroll):
    repo, phone, _, policy, _, profile, create_task, runner = body_scroll
    policy.max_detail_visits = 2
    profile.detail_body_swipes = 2
    phone.detail_frames = [synthetic_body_frame(missing=("body",), complete=False)]
    task = create_task()
    for _ in range(3):
        assert repo.consume_retry(task.id, "body_swipes", 4)
    runner().execute(task.id)
    assert phone.detail_scrolls == 1
    assert repo.task(task.id).retry_counts["body_swipes"] == 4
    runner().execute(task.id)
    assert phone.detail_scrolls == 1
    assert repo.task(task.id).retry_counts["body_swipes"] == 4
    assert repo.task(task.id).detail_visits == 2
    assert repo.task(task.id).eligible_count == 0


def test_crash_after_body_budget_commit_does_not_replenish_it(body_scroll, monkeypatch):
    repo, phone, _, policy, _, profile, create_task, runner = body_scroll
    policy.max_detail_visits = 2
    profile.detail_body_swipes = 1
    phone.detail_frames = [synthetic_body_frame(missing=("body",), complete=False)]
    task = create_task()
    swipe = phone.swipe

    def crash_during_swipe(direction):
        raise KeyboardInterrupt("SYNTHETIC process exit after persisted swipe budget")

    monkeypatch.setattr(phone, "swipe", crash_during_swipe)
    with pytest.raises(KeyboardInterrupt):
        runner().execute(task.id)
    assert repo.task(task.id).retry_counts["body_swipes"] == 1
    assert phone.detail_scrolls == 0
    monkeypatch.setattr(phone, "swipe", swipe)
    runner().execute(task.id)
    assert phone.detail_scrolls == 1
    assert repo.task(task.id).retry_counts["body_swipes"] == 2


@pytest.mark.parametrize(("failure_label", "expected_swipes"), [
    ("detail_initial", 0), ("detail_body_scroll_1", 1),
])
def test_body_evidence_failure_stops_before_any_further_action(
    body_scroll, failure_label, expected_swipes
):
    repo, phone, _, _, evidence, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    phone.detail_frames = [synthetic_body_frame(missing=("body",), complete=False)]
    task = create_task()

    class FailingFrameEvidence:
        def save(self, snapshot, **kwargs):
            if kwargs["label"] == failure_label:
                raise EvidenceError("SYNTHETIC evidence storage failure")
            return evidence.save(snapshot, **kwargs)

    with pytest.raises(EvidenceError):
        runner(FailingFrameEvidence()).execute(task.id)
    assert phone.detail_scrolls == expected_swipes
    assert len(phone.writes) == expected_swipes
    assert repo.task(task.id).retry_counts.get("body_swipes", 0) == expected_swipes
    assert repo.observations(task.id) == []
    assert repo.task(task.id).status == "paused"


@pytest.fixture
def filter_menu(recovery, monkeypatch):
    repo, phone, _, _, _, create_task, runner = recovery
    profile = runner().profile
    profile.pages["filter"] = PageRule(all=[Selector(resource_id="synthetic/filter-menu")])
    profile.actions.update({
        "filter_entry": Selector(resource_id="synthetic/filter-entry"),
        "image_text_filter": Selector(resource_id="synthetic/filter-choice"),
        "filter_confirm": Selector(resource_id="synthetic/filter-confirm"),
    })
    profile.image_text_selected_marker = Selector(
        resource_id="synthetic/filter-choice", selected=True
    )
    phone.filter_selected = False
    phone.reject_filter_selection = False
    phone.duplicate_filter_marker = False
    phone.filter_confirmed = False
    phone.filter_entries = phone.filter_selections = phone.filter_confirmations = 0
    phone.crash_confirm = False
    capture, click, press = phone.capture, phone.click, phone.press

    def menu_capture():
        snap = capture()
        if phone.page == "results":
            snap.xml = snap.xml.replace(
                "</hierarchy>", node("filter-entry", FILTER_ENTRY) + "</hierarchy>"
            )
        elif phone.page == "filter":
            root = ET.fromstring('<hierarchy synthetic="true"/>')
            root.append(ET.fromstring(node("filter-menu")))
            choice = ET.fromstring(node("filter-choice", FILTER_CHOICE, "SYNTHETIC image text"))
            choice.set("selected", "true" if phone.filter_selected else "false")
            root.append(choice)
            if phone.filter_selected and phone.duplicate_filter_marker:
                root.append(ET.fromstring(ET.tostring(choice)))
            root.append(ET.fromstring(node("filter-confirm", FILTER_CONFIRM)))
            snap.xml = ET.tostring(root, encoding="unicode")
        return snap

    def menu_click(bounds):
        if bounds in {FILTER_ENTRY, FILTER_CHOICE, FILTER_CONFIRM}:
            phone._write("click", bounds)
            if bounds == FILTER_ENTRY:
                assert phone.page == "results"
                phone.filter_entries += 1
                phone.page = "filter"
            elif bounds == FILTER_CHOICE:
                assert phone.page == "filter"
                phone.filter_selections += 1
                phone.filter_selected = not phone.reject_filter_selection
                # Selecting keeps the menu open, as in the observed App flow.
            else:
                assert phone.page == "filter"
                if phone.crash_confirm:
                    phone.crash_confirm = False
                    raise KeyboardInterrupt("SYNTHETIC exit while confirming filter menu")
                phone.filter_confirmations += 1
                phone.filter_confirmed = True
                phone.page = "results"
        else:
            if bounds == SUBMIT:
                phone.filter_confirmed = False
            if bounds in {card_bounds(i) for i in (0, 1)}:
                assert phone.filter_confirmed, "Must verify and confirm filter before traversal"
            click(bounds)

    def menu_back(key):
        if phone.page == "filter":
            assert key == "back"
            phone._write("press", key)
            phone.page = "results"
        else:
            press(key)

    monkeypatch.setattr(phone, "capture", menu_capture)
    monkeypatch.setattr(phone, "click", menu_click)
    monkeypatch.setattr(phone, "press", menu_back)
    return repo, phone, profile, create_task, runner


@pytest.mark.parametrize("initially_selected", [False, True])
def test_filter_menu_verifies_selected_state_before_confirming(filter_menu, initially_selected):
    repo, phone, _, create_task, runner = filter_menu
    phone.filter_selected = initially_selected
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.filter_entries == 1
    assert phone.filter_selections == (0 if initially_selected else 1)
    assert phone.filter_confirmations == 1
    assert phone.opened_note_ids == [DEFAULT_NOTE_ID]
    with repo.sessions() as session:
        verified = session.scalar(select(Event).where(Event.kind == "image_text_filter_verified"))
    assert verified is not None


@pytest.mark.parametrize("failure", ["unselected", "duplicate_after", "duplicate_before"])
def test_unconfirmed_or_ambiguous_filter_never_reaches_collection(filter_menu, failure):
    repo, phone, _, create_task, runner = filter_menu
    phone.reject_filter_selection = failure == "unselected"
    phone.duplicate_filter_marker = failure != "unselected"
    phone.filter_selected = failure == "duplicate_before"
    task = create_task()
    runner().execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert phone.filter_entries == 1
    assert phone.filter_selections == (0 if failure == "duplicate_before" else 1)
    assert phone.filter_confirmations == 0
    assert phone.page == "filter"
    assert phone.opened_note_ids == []
    assert repo.task(task.id).detail_visits == 0
    assert repo.observations(task.id) == []
    with repo.sessions() as session:
        failure_evidence = session.scalar(select(Event).where(Event.kind == "failure_evidence"))
    assert failure_evidence is not None


@pytest.mark.parametrize("initial_page", ["results", "filter"])
def test_restart_from_results_or_filter_returns_to_search_before_input(filter_menu, initial_page):
    repo, phone, _, create_task, runner = filter_menu
    phone.page = initial_page
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    first_input = next(i for i, (_, action, _) in enumerate(phone.writes) if action == "input_text")
    backs = [w for w in phone.writes[:first_input] if w[1] == "press"]
    assert len(backs) == (2 if initial_page == "filter" else 1)
    assert phone.search_submissions == 1


def test_interrupted_filter_confirmation_resumes_by_observing_menu_and_researching(filter_menu):
    repo, phone, _, create_task, runner = filter_menu
    phone.crash_confirm = True
    task = create_task()
    with pytest.raises(KeyboardInterrupt):
        runner().execute(task.id)
    assert phone.page == "filter"
    assert repo.task(task.id).pending_step == "action:filter_confirm"
    assert phone.filter_confirmations == 0
    assert phone.opened_note_ids == []
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.search_submissions == 2
    assert phone.filter_entries == 2
    assert phone.filter_selections == 1
    assert phone.filter_confirmations == 1
    assert repo.task(task.id).retry_counts["action:filter_confirm"] == 1


def test_legacy_single_action_filter_still_returns_directly_to_results(recovery, monkeypatch):
    repo, phone, _, _, _, create_task, runner = recovery
    profile = runner().profile
    profile.actions["image_text_filter"] = Selector(resource_id="synthetic/legacy-filter")
    capture, click = phone.capture, phone.click
    clicks = []

    def legacy_capture():
        snap = capture()
        if phone.page == "results":
            snap.xml = snap.xml.replace(
                "</hierarchy>", node("legacy-filter", FILTER_CHOICE) + "</hierarchy>"
            )
        return snap

    def legacy_click(bounds):
        if bounds == FILTER_CHOICE:
            assert phone.page == "results"
            clicks.append(bounds)
            phone._write("click", bounds)
        else:
            click(bounds)

    monkeypatch.setattr(phone, "capture", legacy_capture)
    monkeypatch.setattr(phone, "click", legacy_click)
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    assert clicks == [FILTER_CHOICE]


@pytest.mark.parametrize(("prefix", "display", "ambiguous", "expected"), [
    ("", "{keyword}", False, True),
    ("搜索, ", "搜索, {keyword}", False, True),
    ("搜索, ", "其他, {keyword}", False, False),
    ("搜索, ", "搜索, 搜索, {keyword}", False, False),
    ("搜索, ", "搜索, SYNTHETIC 中文", False, False),
    ("搜索, ", " 搜索, {keyword}", False, False),
    ("搜索, ", "搜索, {keyword} ", False, False),
    ("", "搜索, {keyword}", False, False),
    ("", "", False, False),
    ("搜索, ", "搜索, {keyword}", True, False),
])
def test_keyword_readback_requires_exact_unique_text(
    recovery, monkeypatch, prefix, display, ambiguous, expected
):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    controller = runner()
    controller.profile.search_input_text_prefix = prefix
    capture = phone.capture

    def displayed_input():
        snapshot = capture()
        if phone.page == "search" and phone.keyword:
            root = ET.fromstring(snapshot.xml)
            field = root.find("node[@resource-id='synthetic/search-input']")
            assert field is not None
            field.set("text", display.format(keyword=phone.keyword))
            # A matching description cannot rescue a wrong or absent text value.
            field.set("content-desc", prefix + phone.keyword)
            if ambiguous:
                root.append(ET.fromstring(ET.tostring(field)))
            snapshot.xml = ET.tostring(root, encoding="unicode")
        return snapshot

    monkeypatch.setattr(phone, "capture", displayed_input)
    controller.execute(task.id)
    assert [args for _, name, args in phone.writes if name == "input_text"] == [(task.keyword,)]
    if expected:
        assert_collected(repo, task)
        assert phone.search_submissions == 1
    else:
        assert repo.task(task.id).status == "paused"
        assert repo.task(task.id).observation_count == 0
        assert phone.search_submissions == 0
        assert phone.opened_note_ids == []


def test_temporary_capture_disconnect_recovers_without_an_operator(recovery):
    repo, phone, clock, _, evidence, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1
    runner().execute(task.id)
    assert_collected(repo, task)
    assert clock.elapsed >= 5
    assert repo.task(task.id).retry_counts.get("read_state", 0) >= 1
    for observation in repo.observations(task.id):
        for manifest in observation["evidence"]:
            assert evidence.verify(manifest)


def test_app_crash_restarts_and_replays_search_in_the_same_run(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.crash_on_candidate = True
    runner().execute(task.id)
    assert phone.crashes == 1
    assert_collected(repo, task)
    assert phone.search_submissions >= 2
    assert repo.task(task.id).detail_visits >= 2


def test_exhausted_action_budget_cannot_be_replenished_by_resume(recovery):
    repo, phone, _, policy, _, create_task, runner = recovery
    task = create_task()
    phone.fail_launch = True
    runner().execute(task.id)
    assert phone.launch_failures == 1 + policy.max_retries
    prior_budget = dict(repo.task(task.id).retry_counts)
    for _ in range(3):
        runner().execute(task.id)
    assert phone.launch_failures == 1 + policy.max_retries
    assert repo.task(task.id).retry_counts == prior_budget
    assert repo.task(task.id).eligible_count == 0


def test_initial_rate_limit_waits_then_probes_once_without_resume(recovery):
    repo, phone, clock, policy, _, create_task, runner = recovery
    policy.cooldown_seconds = phone.cooldown = 1800
    task = create_task()
    phone.limit_on_candidate = True
    runner().execute(task.id)
    assert_collected(repo, task)
    assert clock.elapsed >= phone.limit_started_at + 1800
    assert len(phone.rate_captures) == 1
    assert not [
        action for action in phone.writes
        if phone.limit_started_at < action[0] < phone.limit_started_at + 1800
    ]
    assert repo.status(task.id)["policy_states"] == []


def test_rate_limit_present_after_one_probe_requires_manual_attention(recovery):
    repo, phone, clock, policy, _, create_task, runner = recovery
    task = create_task()
    phone.limit_on_candidate = phone.limit_persists = True
    runner().execute(task.id)
    assert repo.task(task.id).status == "needs_attention"
    assert len(phone.rate_captures) == 2
    assert phone.rate_captures[1] >= phone.rate_captures[0] + policy.cooldown_seconds
    assert clock.elapsed >= phone.limit_started_at + policy.cooldown_seconds
    states = repo.status(task.id)["policy_states"]
    assert states and all(s["status"] == "manual" and s["probe_used"] for s in states)
    writes, captures = len(phone.writes), phone.capture_count
    runner().execute(task.id)
    assert len(phone.writes) == writes
    assert phone.capture_count == captures


def test_rate_limit_survives_failure_to_save_its_evidence(recovery):
    repo, phone, _, policy, evidence, create_task, runner = recovery
    task = create_task()
    phone.limit_on_candidate = True

    class FailingAlertEvidence:
        def save(self, snapshot, **kwargs):
            if 'resource-id="synthetic/rate-limited"' in snapshot.xml:
                raise EvidenceError("SYNTHETIC disk full while saving rate-limit screen")
            return evidence.save(snapshot, **kwargs)

    try:
        runner(FailingAlertEvidence()).execute(task.id)
    except EvidenceError:
        pass  # Propagation is allowed; persisted restriction is the invariant.
    states = repo.status(task.id)["policy_states"]
    assert states, "Recognized restriction must survive even when its evidence write fails"
    for state in states:
        assert state["status"] == "cooldown"
        until = aware(datetime.fromisoformat(state["until"]))
        assert until >= phone.clock.origin + timedelta(
            seconds=phone.limit_started_at + policy.cooldown_seconds
        )
    assert repo.task(task.id).eligible_count == 0


def test_unknown_page_blocks_a_new_task_in_the_same_session(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    first = create_task()
    phone.page = "unknown"
    runner().execute(first.id)
    assert repo.task(first.id).status in {"paused", "needs_attention"}
    states = repo.status(first.id)["policy_states"]
    assert any(s["status"] == "manual" for s in states)
    second = create_task()
    writes = len(phone.writes)
    runner().execute(second.id)
    assert len(phone.writes) == writes
    assert repo.task(second.id).status == "needs_attention"
    assert repo.task(second.id).eligible_count == 0


def test_interrupted_probe_is_not_repeated_after_process_restart(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    scopes = [
        "platform:xiaohongshu", f"session:{task.session_ref}", f"device:{task.serial_hash}"
    ]
    repo.block(scopes, reason="rate_limited", until=phone.clock.now(), probe_used=True)
    runner().execute(task.id)
    assert repo.task(task.id).status == "needs_attention"
    assert phone.writes == []
    assert phone.capture_count == 0


def test_multiple_pages_are_collected_without_losing_progress(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    ids = ["1" * 24, "2" * 24, "3" * 24]
    phone.result_pages = [[note_id] for note_id in ids]
    task = create_task(target=3)
    runner().execute(task.id)
    assert_collected(repo, task, count=3)
    assert phone.opened_note_ids == ids
    assert repo.task(task.id).list_swipes == 2
    assert {row["note_id"] for row in repo.observations(task.id)} == set(ids)


@pytest.mark.parametrize("cards_per_page", [1, 2])
def test_traversal_reuses_checked_results_without_extra_capture_or_faster_actions(
    recovery, monkeypatch, cards_per_page,
):
    repo, phone, _, policy, evidence, create_task, runner = recovery
    policy.action_interval = 3
    ids = ["1" * 24, "2" * 24, "3" * 24]
    phone.result_pages = [ids[index:index + cards_per_page]
                          for index in range(0, len(ids), cards_per_page)]
    captured_pages = []
    capture = phone.capture

    def observed_capture():
        if not phone._state_only:
            captured_pages.append(phone.page)
        return capture()

    monkeypatch.setattr(phone, "capture", observed_capture)
    task = create_task(target=len(ids))
    runner().execute(task.id)

    assert_collected(repo, task, count=len(ids))
    assert phone.opened_note_ids == ids
    swipes = len(phone.result_pages) - 1
    assert repo.task(task.id).list_swipes == swipes
    # Initial search and traversal read results once each. Subsequently each
    # actual return/scroll needs one checked view, with no immediate second read.
    assert captured_pages.count("results") == 0
    assert phone.read_count >= 2 + len(ids) + swipes
    assert captured_pages.count("detail") == len(ids)
    assert all(later[0] - earlier[0] >= policy.action_interval
               for earlier, later in zip(phone.writes, phone.writes[1:], strict=False))
    rows = repo.observations(task.id)
    assert len(rows) == len(ids)
    assert all(evidence.verify(item) for row in rows for item in row["evidence"])


def test_pause_after_checked_return_discards_view_even_when_reusing_runner(
    recovery, monkeypatch,
):
    repo, phone, _, _, _, create_task, runner = recovery
    first_id, second_id, third_id = "1" * 24, "2" * 24, "3" * 24
    phone.result_pages = [[first_id, second_id], [third_id]]
    task = create_task(target=3)
    pause_after_return = False
    capture = phone.capture

    def pause_after_results_observation():
        nonlocal pause_after_return
        snap = capture()
        if phone.page == "results" and repo.task(task.id).observation_count == 1:
            pause_after_return = True
        return snap

    monkeypatch.setattr(phone, "capture", pause_after_results_observation)
    controller = runner(stop_requested=lambda: pause_after_return)
    controller.execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert phone.page == "results"
    assert phone.opened_note_ids == [first_id]
    visits = repo.task(task.id).detail_visits

    # The next candidate used to occupy the second slot. Resuming the same
    # Runner object must still discard the checked view and search afresh.
    monkeypatch.setattr(phone, "capture", capture)
    pause_after_return = False
    phone.result_pages = [[third_id, second_id], [first_id]]
    controller.execute(task.id)
    assert_collected(repo, task, count=3)
    assert phone.search_submissions == 2
    assert phone.opened_note_ids == [first_id, third_id, second_id]
    assert repo.task(task.id).detail_visits == visits + 2


@pytest.mark.parametrize("transition", ["return", "scroll"])
def test_results_transition_checks_rate_limit_before_reusing_the_view(
    recovery, monkeypatch, transition,
):
    repo, phone, _, policy, _, create_task, runner = recovery
    first_id, second_id = "1" * 24, "2" * 24
    phone.result_pages = [[first_id], [second_id]]
    phone.limit_persists = True
    operation_name = "press" if transition == "return" else "swipe"
    operation = getattr(phone, operation_name)

    def limited_transition(*args):
        operation(*args)
        phone.page = "rate_limited"
        phone.limit_started_at = phone.clock.elapsed

    monkeypatch.setattr(phone, operation_name, limited_transition)
    task = create_task(target=2)
    runner().execute(task.id)
    stopped = repo.task(task.id)
    assert stopped.status == "needs_attention"
    assert stopped.stop_reason == "rate_limit_after_probe"
    assert stopped.observation_count == stopped.eligible_count == 1
    assert phone.opened_note_ids == [first_id]
    assert len(phone.rate_captures) == 2
    assert phone.rate_captures[1] >= phone.rate_captures[0] + policy.cooldown_seconds


def test_restart_researches_reordered_results_instead_of_reusing_positions(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    first_id, second_id, third_id = "1" * 24, "2" * 24, "3" * 24
    phone.result_pages = [[first_id, second_id], [third_id]]
    task = create_task(target=3)

    def crash_after_committed_observation():
        if repo.task(task.id).observation_count >= 1:
            raise KeyboardInterrupt("SYNTHETIC abrupt process exit after committed note")
        return False

    with pytest.raises(KeyboardInterrupt):
        runner(stop_requested=crash_after_committed_observation).execute(task.id)
    assert repo.task(task.id).observation_count == 1
    assert repo.task(task.id).status == "running"
    prior_visits = repo.task(task.id).detail_visits

    # The old first card has moved to another page; a persisted index would skip
    # the new first card. The new process must search and inspect fresh results.
    phone.result_pages = [[third_id, second_id], [first_id]]
    runner().execute(task.id)
    assert_collected(repo, task, count=3)
    assert phone.search_submissions == 2
    assert phone.opened_note_ids == [first_id, third_id, second_id]
    assert repo.task(task.id).detail_visits == prior_visits + 2
    assert {row["note_id"] for row in repo.observations(task.id)} == {
        first_id, second_id, third_id,
    }
    with repo.sessions() as session:
        statuses = list(session.scalars(select(Run.status).where(Run.task_id == task.id)))
    assert sorted(statuses) == ["collected_awaiting_review", "interrupted"]


def test_operator_pause_and_resume_preserve_committed_notes_and_budgets(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    first_id, second_id = "1" * 24, "2" * 24
    phone.result_pages = [[first_id], [second_id]]
    task = create_task(target=2)
    pause_sent = False

    def request_persisted_pause_after_one_note():
        nonlocal pause_sent
        if not pause_sent and repo.task(task.id).observation_count >= 1:
            pause_sent = True
            repo.request_pause(task.id)
        return False

    runner(stop_requested=request_persisted_pause_after_one_note).execute(task.id)
    paused = repo.task(task.id)
    assert pause_sent and paused.pause_requested
    assert paused.status == "paused"
    assert paused.stop_reason == "operator_pause"
    assert paused.observation_count == paused.eligible_count == 1
    assert paused.detail_visits == 1
    saved_id = repo.observations(task.id)[0]["id"]

    runner().execute(task.id)
    resumed = repo.task(task.id)
    assert not resumed.pause_requested
    assert_collected(repo, task, count=2)
    assert saved_id in {row["id"] for row in repo.observations(task.id)}
    assert resumed.detail_visits >= paused.detail_visits + 1
    assert phone.search_submissions == 2
    assert {row["note_id"] for row in repo.observations(task.id)} == {first_id, second_id}


def test_persistent_no_new_results_ends_partial_with_finite_swipes(recovery):
    repo, phone, _, policy, _, create_task, runner = recovery
    task = create_task(target=3)
    runner().execute(task.id)
    partial = repo.task(task.id)
    assert partial.status == "paused"
    assert partial.stop_reason == "consecutive_no_progress:10"
    assert partial.observation_count == partial.eligible_count == 1
    assert partial.consecutive_no_progress == 10
    assert partial.list_swipes == 10
    assert partial.list_swipes < policy.max_list_swipes
    assert phone.opened_note_ids == [DEFAULT_NOTE_ID]
    prior = (partial.no_progress, partial.list_swipes, partial.detail_visits)
    runner().execute(task.id)
    resumed = repo.task(task.id)
    assert resumed.status == "paused"
    assert resumed.stop_reason == "consecutive_no_progress:10"
    assert resumed.list_swipes > prior[1]
    assert resumed.observation_count == resumed.eligible_count == 1


def test_different_note_ids_with_identical_text_are_not_merged(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    first_id, second_id = "a" * 24, "b" * 24
    # Only IDs differ in the detail fixture. Title/body/author remain identical.
    phone.result_pages = [[first_id, second_id]]
    task = create_task(target=2)
    runner().execute(task.id)
    assert_collected(repo, task, count=2)
    rows = repo.observations(task.id)
    assert len({row["fingerprint"] for row in rows}) == 1
    assert len({row["id"] for row in rows}) == 2
    assert {row["note_id"] for row in rows} == {first_id, second_id}
    status = repo.status(task.id)["tasks"][0]
    assert status["known_unique"] == 2
    assert {frozenset(group) for group in status["possible_duplicates"]} == {
        frozenset(row["id"] for row in rows)
    }


def test_crash_during_cooldown_keeps_deadline_and_reconciles_the_old_run(recovery):
    repo, phone, clock, policy, _, create_task, runner = recovery
    task = create_task()
    phone.limit_on_candidate = True

    def crash_in_durable_cooldown():
        if repo.task(task.id).status == "cooldown":
            raise KeyboardInterrupt("SYNTHETIC process exit during durable cooldown")
        return False

    with pytest.raises(KeyboardInterrupt):
        runner(stop_requested=crash_in_durable_cooldown).execute(task.id)
    assert repo.task(task.id).status == "cooldown"
    saved_states = repo.status(task.id)["policy_states"]
    assert saved_states and not any(state["probe_used"] for state in saved_states)
    deadline = max(datetime.fromisoformat(state["until"]) for state in saved_states)
    assert clock.now() < deadline
    writes_before_resume = len(phone.writes)

    runner().execute(task.id)
    assert_collected(repo, task)
    assert clock.now() >= deadline
    assert phone.rate_captures == [phone.limit_started_at]
    assert not [
        action for action in phone.writes[writes_before_resume:]
        if action[0] < phone.limit_started_at + policy.cooldown_seconds
    ]
    with repo.sessions() as session:
        runs = list(session.scalars(select(Run).where(Run.task_id == task.id)))
    assert sorted(run.status for run in runs) == ["collected_awaiting_review", "interrupted"]
    assert all(run.ended_at is not None for run in runs)


class SyntheticConnection:
    """Offline manager double; no cloud permission or network operation is used."""

    def __init__(self, repo, task_id, phone, *, healthy=False, repair=None, candidates=1):
        self.repo, self.task_id, self.phone = repo, task_id, phone
        self.healthy, self.repair, self.candidates = healthy, repair, candidates
        self.probes = 0
        self.repairs = 0
        self.attempts = 0

    def transport_healthy(self, *, lock_held):
        assert lock_held is True
        assert self.repo.task(self.task_id).pending_step is None
        self.probes += 1
        return self.healthy

    def ensure_connected(self, *, lock_held, consume_attempt):
        assert lock_held is True
        self.repairs += 1
        assert self.repo.task(self.task_id).pending_step is None
        for _ in range(self.candidates):
            if not consume_attempt():
                raise ConnectionFailure("connection_recovery_exhausted", "SYNTHETIC budget")
            self.attempts += 1
            assert self.repo.task(self.task_id).retry_counts["connection:attempt"] >= self.attempts
        if self.repair is not None:
            self.repair()
        return self.phone.serial


def test_proven_transport_failure_repairs_before_retrying_capture(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    connection = SyntheticConnection(repo, task.id, phone)
    phone.disconnect_captures = 1
    runner(connection=connection).execute(task.id)
    assert_collected(repo, task)
    assert connection.probes == connection.repairs == connection.attempts == 1
    counts = repo.task(task.id).retry_counts
    assert counts["connection:attempt"] == counts["read_state"] == 1
    assert repo.task(task.id).pending_step is None


def test_automation_failure_with_healthy_adb_does_not_update_whitelist(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    connection = SyntheticConnection(repo, task.id, phone, healthy=True)
    phone.disconnect_captures = 1  # Same driver error type as a failed UI service.
    runner(connection=connection).execute(task.id)
    assert_collected(repo, task)
    assert connection.probes == 1
    assert connection.repairs == connection.attempts == 0
    assert "connection:attempt" not in repo.task(task.id).retry_counts


def test_repair_of_uncertain_click_researches_instead_of_replaying_coordinates(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    original_click = phone.click
    interrupted = False
    repaired_at = []

    def fail_after_delivering_click(bounds):
        nonlocal interrupted
        original_click(bounds)
        if tuple(bounds) == CARD and not interrupted:
            interrupted = True
            raise DeviceUnavailable("SYNTHETIC click delivered before transport loss")

    def repair():
        repaired_at.append(len(phone.writes))
        phone.page = "home"

    phone.click = fail_after_delivering_click
    connection = SyntheticConnection(repo, task.id, phone, repair=repair)
    runner(connection=connection).execute(task.id)
    assert_collected(repo, task)
    assert phone.search_submissions == 2
    assert len(repaired_at) == 1
    assert phone.writes[repaired_at[0]][1] == "start_app"
    assert repo.task(task.id).retry_counts["connection:attempt"] == 1


def test_connection_candidate_budget_survives_failure_and_new_runner(recovery):
    repo, phone, _, policy, _, create_task, runner = recovery
    task = create_task()
    phone.fail_launch = True

    def failed():
        raise ConnectionFailure("offline", "SYNTHETIC unavailable")

    first = SyntheticConnection(repo, task.id, phone, candidates=3, repair=failed)
    runner(connection=first).execute(task.id)
    assert first.attempts == policy.max_retries + 1
    assert repo.task(task.id).stop_reason == "connection:offline"
    second = SyntheticConnection(repo, task.id, phone)
    runner(connection=second).execute(task.id)
    assert second.repairs == 1 and second.attempts == 0
    assert repo.task(task.id).status == "paused"
    assert repo.task(task.id).stop_reason == "connection:connection_recovery_exhausted"
    assert repo.task(task.id).retry_counts["connection:attempt"] == 3
    assert repo.task(task.id).observation_count == 0
    before = dict(repo.task(task.id).retry_counts)
    runner(connection=second, stop_requested=lambda: True).execute(task.id)
    assert repo.task(task.id).stop_reason == "operator_pause"
    assert repo.task(task.id).retry_counts == before
    assert second.repairs == 1


@pytest.mark.parametrize("pause_kind", ["operator", "parent_eof"])
def test_pause_during_repair_never_replays_a_pending_operation(recovery, pause_kind):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1
    parent_gone = False
    writes_at_repair = []

    def pause():
        nonlocal parent_gone
        writes_at_repair.append(list(phone.writes))
        if pause_kind == "operator":
            repo.request_pause(task.id)
        else:
            parent_gone = True

    connection = SyntheticConnection(repo, task.id, phone, repair=pause)
    runner(connection=connection, stop_requested=lambda: parent_gone).execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert repo.task(task.id).stop_reason == "operator_pause"
    assert repo.task(task.id).retry_counts["connection:attempt"] == 1
    assert repo.task(task.id).pending_step is None
    assert phone.writes == writes_at_repair[0]
    assert repo.task(task.id).observation_count == 0


def test_crash_after_candidate_predebit_does_not_reset_connection_budget(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1

    def crash():
        raise SystemExit("SYNTHETIC process crash after cloud candidate starts")

    connection = SyntheticConnection(repo, task.id, phone, repair=crash)
    with pytest.raises(SystemExit):
        runner(connection=connection).execute(task.id)
    assert repo.task(task.id).retry_counts["connection:attempt"] == 1
    assert repo.task(task.id).pending_step is None
    # A new process can recover, but it must retain the previous candidate debit.
    phone.disconnect_captures = 1
    replacement = SyntheticConnection(repo, task.id, phone)
    runner(connection=replacement).execute(task.id)
    assert_collected(repo, task)
    assert repo.task(task.id).retry_counts["connection:attempt"] == 2


def test_connection_failure_does_not_persist_exception_text_or_unknown_code(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1
    sentinel = "SYNTHETIC-DO-NOT-PERSIST-SECRET"

    def failure():
        raise ConnectionFailure(sentinel, f"Cloud provider diagnostic contains {sentinel}")

    connection = SyntheticConnection(repo, task.id, phone, repair=failure)
    runner(connection=connection).execute(task.id)
    assert repo.task(task.id).stop_reason == "connection:connection_failed"
    with repo.sessions() as session:
        events = list(session.scalars(select(Event).where(Event.task_id == task.id)))
    assert sentinel not in str([event.detail for event in events])
    assert any(event.kind == "connection_recovery_failed" for event in events)


def test_cancelled_transport_probe_stops_without_attempting_cloud_update(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1
    connection = SyntheticConnection(repo, task.id, phone)

    def cancelled(*, lock_held):
        assert lock_held
        raise ConnectionFailure("cancelled", "SYNTHETIC parent pipe closed")

    connection.transport_healthy = cancelled
    runner(connection=connection).execute(task.id)
    assert repo.task(task.id).stop_reason == "operator_pause"
    assert connection.repairs == 0
    assert "connection:attempt" not in repo.task(task.id).retry_counts


def test_connection_recovery_cannot_change_the_explicit_device_binding(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 1
    connection = SyntheticConnection(repo, task.id, phone)

    def another_serial(*, lock_held, consume_attempt):
        assert lock_held and consume_attempt()
        return "SYNTHETIC-different-device"

    connection.ensure_connected = another_serial
    runner(connection=connection).execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert repo.task(task.id).stop_reason == "connection:device_binding_changed"
    assert repo.task(task.id).observation_count == 0


def test_cli_recovery_callback_observes_both_task_pause_and_parent_stop(recovery, monkeypatch):
    from xhs_mobile import cli

    repo, phone, _, _, _, create_task, _ = recovery
    task = create_task()
    parent_gone = False
    options = {}

    class SyntheticManager:
        auto_enabled = True
        serial = phone.serial

        def __init__(self, settings, device_id, **kwargs):
            assert settings == "SYNTHETIC-settings"
            assert device_id == task.device_id
            options.update(kwargs)

    monkeypatch.setattr(cli, "ConnectionManager", SyntheticManager)
    result = cli.connection_options(
        "SYNTHETIC-settings", repo, task.id, lambda: parent_gone
    )
    assert isinstance(result["connection"], SyntheticManager)
    assert options["stop_requested"]() is False
    repo.request_pause(task.id)
    assert options["stop_requested"]() is True
    repo.start_run(task.id, "SYNTHETIC-profile")
    assert options["stop_requested"]() is False
    parent_gone = True
    assert options["stop_requested"]() is True
    SyntheticManager.auto_enabled = False
    assert cli.connection_options("SYNTHETIC-settings", repo, task.id, lambda: False) == {}


def test_cli_rejects_changed_connection_serial_before_any_unlocked_probe(recovery, monkeypatch):
    from unittest.mock import Mock

    from xhs_mobile import cli

    repo, _, _, _, _, create_task, _ = recovery
    task = create_task()
    manager = Mock(auto_enabled=True, serial="SYNTHETIC-other-device-not-locked")
    monkeypatch.setattr(cli, "ConnectionManager", Mock(return_value=manager))
    with pytest.raises(ValueError, match="自动连接记录与任务绑定的设备不一致"):
        cli.connection_options("SYNTHETIC-settings", repo, task.id, lambda: False)
    manager.transport_healthy.assert_not_called()
    manager.ensure_connected.assert_not_called()
    assert repo.task(task.id).retry_counts == {}


def paused_exhausted_capture(repo, create_task):
    task = create_task()
    run = repo.start_run(task.id, "synthetic-profile")
    for _ in range(3):
        repo.fail_step(task.id, "capture", 2)
    repo.finish(task.id, run, "paused", "capture: persistent retry budget exhausted")
    return task


def test_explicit_read_recovery_checks_normal_page_and_preserves_counters(recovery):
    from uuid import uuid4

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    request = str(uuid4())
    assert runner().check_and_grant_read_retry(task.id, request)["checked"]
    assert phone.capture_count == 1 and not phone.writes
    assert repo.task(task.id).retry_counts["capture"] == 3
    assert repo.task(task.id).observation_count == 0
    assert runner().check_and_grant_read_retry(task.id, request)["checked"]
    assert not runner().check_and_grant_read_retry(task.id, str(uuid4()))["granted"]
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    runner().execute(task.id)
    assert_collected(repo, task)
    assert repo.task(task.id).retry_counts["capture"] == 3


@pytest.mark.parametrize("page", ["unknown", "login", "verification", "restricted", "rate_limited"])
def test_explicit_read_recovery_alert_never_grants_or_clears_policy(recovery, page):
    from uuid import uuid4

    from xhs_mobile.domain import PageError

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    phone.page = page
    phone.limit_started_at = clock.elapsed
    phone.limit_persists = True
    with pytest.raises(PageError, match=f"read_retry_check:{page}"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert not phone.writes
    assert repo.policy_states(repo.task_scopes(task))
    assert repo.task(task.id).retry_counts["capture"] == 3


@pytest.mark.parametrize("block", ["manual", "cooldown", "probe"])
def test_read_retry_existing_policy_rejected_before_device_contact(recovery, block):
    from uuid import uuid4

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    kwargs = {"reason": "SYNTHETIC existing policy", "probe_used": block == "probe"}
    if block == "cooldown":
        kwargs["until"] = clock.now() + timedelta(seconds=30)
    repo.block(repo.task_scopes(task), **kwargs)
    with pytest.raises(DeviceError, match="read_retry_check:policy_blocked"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert phone.capture_count == 0 and not phone.writes


def test_read_retry_capture_and_health_failures_do_not_top_up(recovery):
    from uuid import uuid4

    from xhs_mobile.domain import DeviceError

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    phone.disconnect_captures = 1
    with pytest.raises(DeviceError, match="read_retry_check:automation_read_failed"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    phone.health = lambda: {"ok": False, "checks": {"adb_state": "device"}}
    with pytest.raises(DeviceError, match="read_retry_check:automation_check_failed"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    phone.health = lambda: {"ok": False, "checks": {"adb_state": "offline"}}
    with pytest.raises(DeviceError, match="read_retry_check:offline"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert repo.task(task.id).retry_counts["capture"] == 3


def test_read_retry_cancelled_before_grant_does_not_clear_pause(recovery):
    from uuid import uuid4

    from xhs_mobile.domain import DeviceError

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    original = phone.capture

    def capture_and_pause():
        snap = original()
        repo.request_pause(task.id)
        return snap

    phone.capture = capture_and_pause
    with pytest.raises(DeviceError, match="read_retry_check:cancelled"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert repo.task(task.id).pause_requested


def test_regular_resume_never_grants_read_retry(recovery):
    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    runner().execute(task.id)
    assert not repo.capture_retry_status(task.id)["exhausted"]
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert phone.capture_count >= 1
    assert_collected(repo, task)


def test_manual_block_after_failed_check_can_be_explicitly_resolved(recovery):
    from uuid import uuid4

    from xhs_mobile.domain import PageError

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    phone.page = "verification"
    with pytest.raises(PageError):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    phone.page = "home"
    with pytest.raises(DeviceError, match="policy_blocked"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert runner().check_and_grant_read_retry(
        task.id, str(uuid4()), acknowledge=True,
    )["checked"]
    assert not repo.policy_states(repo.task_scopes(task))
    assert repo.task(task.id).status == "paused"
    runner().execute(task.id)
    assert_collected(repo, task)


def test_read_recovery_cooldown_cannot_skip_and_expired_probe_is_persistent(recovery):
    from uuid import uuid4

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    repo.block(repo.task_scopes(task), reason="rate_limited",
               until=clock.now() + timedelta(seconds=30))
    repo.update_state(task.id, "", "cooldown", "rate_limited")
    with pytest.raises(DeviceError, match="policy_blocked"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()), acknowledge=True)
    assert phone.capture_count == 0
    clock.sleep(31)
    phone.disconnect_captures = 1
    with pytest.raises(DeviceError, match="automation_read_failed"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert all(state.probe_used for state in repo.policy_states(repo.task_scopes(task)))
    reads = phone.capture_count
    with pytest.raises(DeviceError, match="policy_blocked"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert phone.capture_count == reads
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0
    assert runner().check_and_grant_read_retry(
        task.id, str(uuid4()), acknowledge=True,
    )["checked"]


def test_expired_cooldown_still_limited_requires_manual_processing(recovery):
    from uuid import uuid4

    from xhs_mobile.domain import PageError

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = paused_exhausted_capture(repo, create_task)
    repo.block(repo.task_scopes(task), reason="rate_limited",
               until=clock.now() - timedelta(seconds=1))
    phone.page, phone.limit_persists = "rate_limited", True
    with pytest.raises(PageError, match="rate_limited"):
        runner().check_and_grant_read_retry(task.id, str(uuid4()))
    assert all(state.status == "manual" for state in repo.policy_states(repo.task_scopes(task)))
    assert repo.task(task.id).stop_reason == "rate_limit_after_probe"
    assert repo.capture_retry_status(task.id)["granted_credits"] == 0


def test_explicit_read_recovery_reconciles_stale_pending_capture_once(recovery):
    from uuid import uuid4

    repo, phone, clock, policy, evidence, create_task, runner = recovery
    task = create_task()
    run_id = repo.start_run(task.id, "SYNTHETIC crashed owner")
    repo.fail_step(task.id, "capture", 2)
    repo.fail_step(task.id, "capture", 2)
    assert repo.begin_step(task.id, "capture", 2)
    assert not repo.capture_retry_status(task.id)["exhausted"]
    request = str(uuid4())
    assert runner().check_and_grant_read_retry(task.id, request)["checked"]
    assert repo.task(task.id).pending_step is None
    assert repo.task(task.id).retry_counts["capture"] == 3
    with repo.sessions() as session:
        assert session.get(Run, run_id).status == "interrupted"
    assert runner().check_and_grant_read_retry(task.id, request)["checked"]
    assert repo.task(task.id).retry_counts["capture"] == 3
    runner().execute(task.id)
    assert_collected(repo, task)


def test_tenth_consecutive_read_failure_pauses_once_and_explicit_resume_keeps_history(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    task = create_task()
    phone.disconnect_captures = 10
    runner().execute(task.id)
    stopped = repo.task(task.id)
    assert stopped.status == "paused"
    assert stopped.stop_reason == "consecutive_read_failures:10"
    assert stopped.consecutive_read_failures == 10
    assert stopped.retry_counts["read_state"] == 10
    assert phone.read_count == 10
    assert not phone.opened_note_ids
    with repo.sessions() as session:
        reminders = list(session.scalars(select(Event).where(Event.kind == "read_anomaly")))
    assert len(reminders) == 1
    # No task or connection budget is automatically replenished on continuation.
    repo.consume_retry(task.id, "connection:attempt", 3)
    runner().execute(task.id)
    assert_collected(repo, task)
    continued = repo.task(task.id)
    assert continued.retry_counts["read_state"] == 10
    assert continued.retry_counts["connection:attempt"] == 1
    assert continued.consecutive_read_failures == 0


def test_scattered_read_failures_over_ten_keep_collecting(recovery, monkeypatch):
    repo, phone, _, _, _, create_task, runner = recovery
    phone.result_pages = [[f"{index:024x}"] for index in range(1, 7)]
    original_read = phone.read_state
    calls = 0

    def intermittent_read():
        nonlocal calls
        calls += 1
        if calls <= 40 and calls % 2:
            raise DeviceUnavailable("SYNTHETIC intermittent readable-state failure")
        return original_read()

    monkeypatch.setattr(phone, "read_state", intermittent_read)
    task = create_task(target=6)
    runner().execute(task.id)
    assert_collected(repo, task, 6)
    assert repo.task(task.id).retry_counts["read_state"] == 20
    assert repo.task(task.id).consecutive_read_failures == 0


def test_ten_loading_timeouts_pause_without_lifetime_wait_lock(recovery, monkeypatch):
    repo, phone, clock, _, _, create_task, runner = recovery
    profile = runner().profile
    profile.pages["loading"] = PageRule(all=[Selector(resource_id="synthetic/loading")])
    original_read = phone.read_state

    def loading_state():
        state = original_read()
        state.xml = '<hierarchy synthetic="true">' + node("loading") + '</hierarchy>'
        return state

    monkeypatch.setattr(phone, "read_state", loading_state)
    task = create_task()
    runner().execute(task.id)
    stopped = repo.task(task.id)
    assert stopped.stop_reason == "consecutive_no_progress:10"
    assert stopped.consecutive_no_progress == 10
    assert stopped.consecutive_read_failures == 0
    assert stopped.retry_counts["wait:launch"] == 10
    assert clock.elapsed < 100
    monkeypatch.setattr(phone, "read_state", original_read)
    runner().execute(task.id)
    assert_collected(repo, task)
    assert repo.task(task.id).retry_counts["wait:launch"] == 10


def test_reliable_identity_skips_saved_note_before_full_detail_capture(recovery):
    repo, phone, _, _, _, create_task, runner = recovery
    first, second = "1" * 24, "2" * 24
    phone.result_pages = [[first]]
    task = create_task()

    def identified_runner():
        instance = runner()
        instance.identity_reader = lambda *_: {
            "note_id": phone.current_note_id,
            "canonical_url": f"https://www.xiaohongshu.com/explore/{phone.current_note_id}",
            "identity_source": "SYNTHETIC fresh clipboard proof",
            "identity_proof": {"source_kind": "synthetic", "fresh": True},
        }
        return instance

    identified_runner().execute(task.id)
    repo.raise_target(task.id, 2)
    phone.result_pages = [[first, second]]
    before = phone.capture_count
    identified_runner().execute(task.id)
    assert_collected(repo, task, 2)
    assert phone.capture_count - before == 1
    assert phone.opened_note_ids == [first, first, second]
    records = repo.observations(task.id)
    assert len({row["note_id"] for row in records}) == 2
    assert all(row["evidence"][0]["metadata"]["identity_proof"]["fresh"] for row in records)
    with repo.sessions() as session:
        skipped = session.scalar(select(Event).where(Event.kind == "known_note_skipped"))
    assert skipped.detail["before_detailed_capture"]


def test_readable_body_without_completeness_marker_never_scrolls_to_prove_completeness(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    profile.fields.pop("published_at")
    phone.detail_frames = [synthetic_body_frame(complete=False)]
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.detail_scrolls == 0
    row = repo.observations(task.id)[0]
    assert row["data"]["body_complete"] is None
    assert row["data"]["completeness_status"] == "not_assessed"
    assert not any("complete" in warning for warning in row["data"]["warnings"])
    assert row["data"]["fields"]["body"]["evidence_ref"]


def test_expanded_observed_body_replaces_shorter_readable_text_without_completeness_claim(
    body_scroll, monkeypatch,
):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.fields.pop("published_at")
    profile.actions["expand_body"] = Selector(resource_id="synthetic/expand-body")
    expansion = (100, 800, 300, 870)
    initial = synthetic_body_frame(complete=False, body="SYNTHETIC short …")
    initial = initial.replace('</hierarchy>', node("expand-body", expansion) + '</hierarchy>')
    phone.detail_frames = [initial, synthetic_body_frame(
        complete=False, body="SYNTHETIC longer actual body obtained from expanded UI text …",
    )]
    click = phone.click

    def expand(bounds):
        if tuple(bounds) == expansion:
            phone._write("expand_body", bounds)
            phone.detail_position = 1
        else:
            click(bounds)

    monkeypatch.setattr(phone, "click", expand)
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    row = repo.observations(task.id)[0]
    assert row["data"]["fields"]["body"]["raw"].startswith("SYNTHETIC longer actual")
    assert row["data"]["body_complete"] is None
    assert row["data"]["fields"]["body"]["evidence_ref"] == row["evidence"][1]["manifest_path"]
    assert phone.detail_scrolls == 0


def test_supplemental_time_is_anchored_to_its_own_evidence_without_merging_base_fields(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    initial = synthetic_body_frame(complete=False, body="SYNTHETIC initial readable body")
    later = synthetic_body_frame(missing=("body",), complete=False)
    later = later.replace(
        '</hierarchy>', node("published-at", text="编辑于昨天上海") + '</hierarchy>',
    )
    phone.detail_frames = [initial, later]
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    row = repo.observations(task.id)[0]
    fields = row["data"]["fields"]
    assert fields["body"]["raw"] == "SYNTHETIC initial readable body"
    assert fields["body"]["evidence_ref"] == row["evidence"][0]["manifest_path"]
    assert fields["published_at"]["raw"] == "编辑于昨天上海"
    assert fields["published_at"]["evidence_ref"] == row["evidence"][1]["manifest_path"]
    assert row["data"]["time_kind"] == "edited"
    assert phone.detail_scrolls == 1


def test_detail_change_between_identity_proof_and_full_frame_drops_copied_identity(
    recovery, monkeypatch,
):
    repo, phone, _, policy, _, _, runner = recovery
    phone.page = "detail"
    phone.body_shift = False
    capture = phone.capture

    def changing_frame():
        snap = capture()
        tree = ET.fromstring(snap.xml)
        for parent in tree.iter():
            for child in list(parent):
                if child.get("resource-id") == "synthetic/note-id":
                    parent.remove(child)
                elif child.get("resource-id") == "synthetic/body" and phone.body_shift:
                    child.set("text", "SYNTHETIC another note appeared after copy")
        snap.xml = ET.tostring(tree, encoding="unicode")
        return snap

    monkeypatch.setattr(phone, "capture", changing_frame)
    task = repo.create_task(
        device_id="synthetic-device", serial=phone.serial, session_ref="synthetic-session",
        keyword="SYNTHETIC changed detail", target=1, policy=policy.model_dump(), mode="current",
    )
    instance = runner()

    def identity_then_change(_, initial_state):
        phone.body_shift = True
        return {"note_id": "1" * 24,
                "canonical_url": "https://www.xiaohongshu.com/explore/" + "1" * 24,
                "state": initial_state, "identity_proof": {"source_kind": "synthetic"}}

    instance.identity_reader = identity_then_change
    instance.execute(task.id)
    assert_collected(repo, task)
    row = repo.observations(task.id)[0]
    assert row["note_id"] is None
    assert row["data"]["identity_status"] == "unverified"
    assert "another note" in row["data"]["fields"]["body"]["raw"]
    assert "identity_proof" not in row["evidence"][0]["metadata"]


def test_system_status_and_count_text_are_not_page_advancement(recovery):
    from xhs_mobile.runner import page_progress_signature

    _, _, _, _, _, _, runner = recovery
    profile = runner().profile

    def state(clock, count, width, title="SYNTHETIC stable title"):
        return UIState(
            '<hierarchy><node package="com.android.systemui" text="' + clock + '"/>'
            '<node package="test.synthetic.app" resource-id="synthetic/card" '
            'bounds="[20,200][1000,700]" text="' + title + '">'
            '<node text="' + count + '" bounds="[20,700][' + width + ',750]"/>'
            '</node></hierarchy>'
        )

    first, second = state("10:00", "99", "70"), state("10:01", "100", "80")
    assert page_progress_signature(first, profile) == page_progress_signature(second, profile)
    assert page_progress_signature(first, profile, candidates_only=True) == page_progress_signature(
        second, profile, candidates_only=True,
    )
    moved = state("10:01", "100", "80", title="SYNTHETIC new candidate title")
    assert page_progress_signature(first, profile) != page_progress_signature(moved, profile)


def test_refreshing_likes_and_system_clock_cannot_reopen_same_viewport_or_reset_streak(
    recovery, monkeypatch,
):
    repo, phone, _, _, _, create_task, runner = recovery
    capture = phone.capture
    counter = 0

    def animated_ui():
        nonlocal counter
        counter += 1
        snap = capture()
        root = ET.fromstring(snap.xml)
        ET.SubElement(root, "node", {"package": "com.android.systemui", "text": str(counter)})
        for card in root.iter():
            if card.get("resource-id") == "synthetic/card":
                ET.SubElement(card, "node", {
                    "text": str(counter), "bounds": f"[20,700][{70 + counter},750]",
                })
        snap.xml = ET.tostring(root, encoding="unicode")
        return snap

    monkeypatch.setattr(phone, "capture", animated_ui)
    task = create_task(target=2)
    runner().execute(task.id)
    row = repo.task(task.id)
    assert row.stop_reason == "consecutive_no_progress:10"
    assert row.list_swipes == 10
    assert row.observation_count == row.eligible_count == 1
    assert phone.opened_note_ids == [DEFAULT_NOTE_ID]


def test_uncalibrated_topics_do_not_trigger_extra_detail_scrolls(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    profile.fields.pop("published_at")
    profile.fields["tags"].platform_topic = False
    phone.detail_frames = [synthetic_body_frame(complete=False)]
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.detail_scrolls == 0
    assert repo.observations(task.id)[0]["data"]["topics_status"] == "unrecognized"


def test_supplemental_author_conflict_cannot_inherit_copied_note_id(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    initial = synthetic_body_frame(missing=("body",), complete=False)
    later = synthetic_body_frame(complete=False)
    later = later.replace('text="SYNTHETIC 作者"', 'text="SYNTHETIC 切换后的作者"')
    phone.detail_frames = [initial, later]
    task = create_task()
    instance = runner()
    instance.identity_reader = lambda *_: {
        "note_id": DEFAULT_NOTE_ID, "canonical_url": "https://www.xiaohongshu.com/explore/"
        + DEFAULT_NOTE_ID,
    }
    instance.execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert "author changed" in repo.task(task.id).stop_reason
    assert repo.observations(task.id) == []


@pytest.fixture
def interrupted_share(recovery, monkeypatch):
    repo, phone, _, _, _, create_task, runner = recovery
    profile = runner().profile
    profile.pages["share"] = PageRule(all=[Selector(resource_id="synthetic/share-sheet")])
    profile.actions["share_close"] = Selector(resource_id="synthetic/share-close")
    close_bounds = (700, 1100, 1000, 1190)
    capture, click = phone.capture, phone.click
    phone.share_close_count = 0

    def share_frame():
        snap = capture()
        if phone.page == "share":
            snap.xml = ('<hierarchy synthetic="true">' + node("share-sheet")
                        + node("share-close", close_bounds) + '</hierarchy>')
        return snap

    def close_share(bounds):
        if tuple(bounds) == close_bounds:
            phone._write("close_share", bounds)
            phone.share_close_count += 1
            phone.page = "detail"
        else:
            click(bounds)

    monkeypatch.setattr(phone, "capture", share_frame)
    monkeypatch.setattr(phone, "click", close_share)
    phone.page = "share"
    task = create_task()
    run = repo.start_run(task.id, "SYNTHETIC old owner paused in share")
    repo.finish(task.id, run, "paused", "operator_pause")
    return repo, phone, task, runner


def test_explicit_resume_closes_known_share_overlay_before_research(interrupted_share):
    repo, phone, task, runner = interrupted_share
    assert phone.page == "share" and phone.writes == []
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.share_close_count == 1
    assert phone.search_submissions == 1
    assert not repo.policy_states(repo.task_scopes(repo.task(task.id)))
    with repo.sessions() as session:
        assert session.scalar(select(Event).where(Event.kind == "share_overlay_recovered"))


def test_pause_request_does_not_close_or_act_on_share_overlay(interrupted_share):
    repo, phone, task, runner = interrupted_share
    runner(stop_requested=lambda: True).execute(task.id)
    assert repo.task(task.id).status == "paused"
    assert phone.page == "share"
    assert phone.share_close_count == 0 and phone.writes == []


def test_current_note_explicit_resume_recovers_known_share_without_search(interrupted_share):
    repo, phone, task, runner = interrupted_share
    with repo.sessions.begin() as session:
        session.get(type(task), task.id).mode = "current"
    runner().execute(task.id)
    assert_collected(repo, task)
    assert phone.share_close_count == 1
    assert phone.search_submissions == 0


def test_long_body_then_third_swipe_time_uses_light_optional_probes_and_exact_evidence(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    profile.fields["tags"].platform_topic = False
    body = "SYNTHETIC long body remains identical during the controlled footer scroll"

    def frame(*, missing=(), timestamp=None):
        tree = ET.fromstring(synthetic_body_frame(missing=missing, complete=False, body=body))
        for parent in tree.iter():
            for child in list(parent):
                if child.get("resource-id") == "synthetic/note-id":
                    parent.remove(child)
        if timestamp:
            tree.append(ET.fromstring(node("published-at", text=timestamp)))
        return ET.tostring(tree, encoding="unicode")

    phone.detail_frames = [
        frame(missing=("body",)), frame(), frame(missing=("title",)),
        frame(missing=("title",), timestamp="08-31广东"),
    ]
    task = create_task()
    instance = runner()
    instance.identity_reader = lambda _, state: {
        "note_id": DEFAULT_NOTE_ID, "state": state,
        "canonical_url": "https://www.xiaohongshu.com/explore/" + DEFAULT_NOTE_ID,
        "identity_source": "SYNTHETIC fresh copy link",
    }
    instance.execute(task.id)
    assert_collected(repo, task)
    row = repo.observations(task.id)[0]
    fields = row["data"]["fields"]
    assert row["note_id"] == DEFAULT_NOTE_ID
    assert fields["body"]["raw"] == body
    assert fields["title"]["raw"] == "SYNTHETIC 测试标题"
    assert fields["published_at"]["raw"] == "08-31广东"
    assert fields["published_at"]["normalized"] is None
    assert row["data"]["time_kind"] == "published"
    assert phone.detail_scrolls == 3
    assert phone.capture_count == 3, "Initial frame, readable body, then confirmed time only"
    assert phone.read_count == 3, "Initial detail plus two lightweight optional field probes"
    assert [item["label"] for item in row["evidence"]] == [
        "detail_initial", "detail_body_scroll_1", "detail_body_scroll_3",
    ]
    assert fields["body"]["evidence_ref"] == row["evidence"][1]["manifest_path"]
    assert fields["published_at"]["evidence_ref"] == row["evidence"][2]["manifest_path"]
    with repo.sessions() as session:
        probes = list(session.scalars(select(Event).where(Event.kind == "optional_field_probe")))
    assert [event.detail["full_capture"] for event in probes] == [False, True]


def test_missing_optional_time_is_bounded_without_additional_screenshots(body_scroll):
    repo, phone, _, _, _, profile, create_task, runner = body_scroll
    profile.detail_body_swipes = 3
    phone.detail_frames = [synthetic_body_frame(complete=False)]
    task = create_task()
    runner().execute(task.id)
    assert_collected(repo, task)
    row = repo.observations(task.id)[0]
    assert phone.detail_scrolls == 3
    assert phone.capture_count == 1
    assert len(row["evidence"]) == 1
    assert row["data"]["fields"]["published_at"]["status"] == "not_readable"
    assert row["data"]["body_complete"] is None
