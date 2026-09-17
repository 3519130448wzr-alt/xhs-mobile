"""Single-controller state machine. Only observed, calibrated UI targets are actionable."""

import hashlib
import re
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol, TypeVar

from xhs_mobile.config import Policy
from xhs_mobile.connection import ConnectionFailure
from xhs_mobile.domain import (
    Device,
    DeviceError,
    EvidenceError,
    EvidenceSink,
    PageError,
    ParsedNote,
    ProfileError,
    Snapshot,
    UIState,
    UnknownPage,
    utcnow,
)
from xhs_mobile.ocr import LocalOCR
from xhs_mobile.parser import candidates, parse_note
from xhs_mobile.profile import Profile, classify, locate, matching, xml_root
from xhs_mobile.repository import READ_STEPS, Repository, aware, identifier

T = TypeVar("T")
NORMAL_PAGES = {"home", "search", "results", "detail", "video", "filter"}


def page_progress_signature(state: Snapshot | UIState, profile: Profile, *,
                            candidates_only: bool = False) -> tuple:
    """Transient UI movement evidence, never a persisted note identity.

    Android status bars and changing engagement counters cannot prove page
    movement. Bounds and meaningful app text/control state can prove it.
    """
    root = xml_root(state)
    roots = matching(root, profile.candidate_selector) if candidates_only else [root]
    ignored = set()
    for name in ("likes", "favorites", "comments", "published_at"):
        rule = profile.fields.get(name)
        if rule:
            ignored.update(id(node) for node in matching(root, rule.selector))
    count_only = re.compile(r"^(?:点赞|收藏|评论|分享)?\s*"
                            r"(?:[0-9]+(?:[.,][0-9]+)*)(?:万|亿|[kKmMwW])?\+?$")

    def visible_text(value: str) -> str:
        stripped = value.strip()
        return "" if count_only.fullmatch(stripped) else stripped

    def walk(node):
        package = node.get("package", "")
        if package and package != profile.app_package:
            return ()
        if node.get("visible-to-user") == "false":
            return ()
        labels = [node.get(name, "").strip() for name in ("text", "content-desc")
                  if node.get(name, "").strip()]
        if id(node) in ignored or (labels and all(count_only.fullmatch(v) for v in labels)):
            return ()
        own = (node.get("resource-id", ""), node.get("class", ""),
               visible_text(node.get("text", "")),
               visible_text(node.get("content-desc", "")), node.get("bounds", ""),
               node.get("selected", ""), node.get("focused", ""))
        return (own, tuple(value for child in node if (value := walk(child))))

    return tuple(walk(node) for node in roots)


class ConnectionRecovery(Protocol):
    """Recovery belongs to the collector which already owns the device lock."""

    def transport_healthy(self, *, lock_held: bool) -> bool: ...

    def ensure_connected(
        self, *, lock_held: bool, consume_attempt: Callable[[], bool]
    ) -> str: ...


class StopRun(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class RecoverSearch(Exception):
    """Discard volatile UI positions and rebuild navigation after a bounded failure."""


class Cooldown(Exception):
    """Policy is already durable; re-enter preflight without further UI actions."""


class Runner:
    def __init__(
        self,
        *,
        repository: Repository,
        device: Device,
        evidence: EvidenceSink,
        profile: Profile,
        stop_requested: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable = utcnow,
        ocr=None,
        connection: ConnectionRecovery | None = None,
        identity_reader: Callable | None = None,
    ):
        self.repo, self.device, self.evidence, self.profile = repository, device, evidence, profile
        self.stop_requested, self.sleep, self.monotonic, self.now = (
            stop_requested,
            sleep,
            monotonic,
            now,
        )
        self.ocr = ocr if ocr is not None else LocalOCR()
        self.connection = connection
        if identity_reader is None:
            from xhs_mobile.identity import read_note_identity
            identity_reader = read_note_identity
        self.identity_reader = identity_reader
        self.last_action = None
        self.last_snapshot: Snapshot | None = None
        self.last_state: UIState | Snapshot | None = None
        self.task_id = self.run_id = ""
        self.scopes: list[str] = []
        self.pending_page_progress: tuple[tuple, str] | None = None

    def _normal_pages(self) -> set[str]:
        if "share" in self.profile.pages and "share_close" in self.profile.actions:
            return NORMAL_PAGES | {"share"}
        return NORMAL_PAGES

    def _close_share_overlay(self, state: Snapshot | UIState, *,
                             expected: set[str]) -> Snapshot | UIState:
        """An explicit run may recover a known share sheet left by a paused run."""
        if classify(state, self.profile) != "share":
            return state
        self._pause_check()
        self._click_action(state, "share_close")
        recovered = self._wait(expected, "resume_share_close")
        self.repo.event(self.task_id, self.run_id, "share_overlay_recovered", {})
        return recovered

    def check_and_grant_read_retry(
        self, task_id: str, request_id: str, *, acknowledge: bool = False,
    ) -> dict:
        """Explicit CLI/UI recovery only; caller owns the exclusive device lock.

        Legacy request IDs remain accepted. This checks one fresh full page and
        existing policy gates without issuing new read credits or starting a run.
        """
        existing_task = self.repo.read_retry_request_task(request_id)
        if existing_task and existing_task != task_id:
            raise ValueError("读取恢复请求已用于另一任务")
        self.repo.reconcile_interrupted_read(task_id)
        self.task_id = task_id
        self.task = self.repo.task(task_id)
        self.policy = Policy.model_validate(self.task.policy)
        self.scopes = self.repo.task_scopes(self.task)
        states = self.repo.policy_states(self.scopes)
        manual = any(state.status == "manual" for state in states)
        if self.task.status not in {"paused", "needs_attention", "cooldown"}:
            raise DeviceError("read_retry_check:task_not_paused")
        if self.task.status != "paused" and not states:
            raise DeviceError("read_retry_check:task_not_paused")
        if manual and not acknowledge:
            raise DeviceError("read_retry_check:policy_blocked")
        if any(state.until is not None and aware(state.until) > self.now() for state in states):
            raise DeviceError("read_retry_check:policy_blocked")
        if not manual and any(state.probe_used for state in states):
            self.repo.block(self.scopes, reason="previous_probe_interrupted", probe_used=True)
            self.repo.update_state(task_id, "", "needs_attention", "previous_probe_interrupted")
            raise DeviceError("read_retry_check:policy_blocked")

        def check_cancelled():
            # An old pause flag is intentionally preserved; a new pause still cancels.
            current = self.repo.task(task_id)
            new_pause = (current.pause_requested and
                         current.updated_at != self.task.updated_at)
            if self.stop_requested() or new_pause:
                raise DeviceError("read_retry_check:cancelled")

        check_cancelled()
        if states:
            self.repo.claim_probe(self.scopes)
        try:
            health = self.device.health()
        except DeviceError as exc:
            raise DeviceError("read_retry_check:automation_check_failed") from exc
        check_cancelled()
        if not health.get("ok"):
            adb_state = health.get("checks", {}).get("adb_state")
            reason = ("offline" if adb_state is not None and adb_state != "device"
                      else "automation_check_failed")
            raise DeviceError(f"read_retry_check:{reason}")
        try:
            snap = self.device.capture()
        except DeviceError as exc:
            raise DeviceError("read_retry_check:automation_read_failed") from exc
        check_cancelled()
        self.last_snapshot = snap
        manifest = self.evidence.save(snap, device_id=self.task.device_id,
                                      run_id=f"read-retry-{request_id}", label="read_retry_check")
        page = classify(snap, self.profile)
        self.repo.event(task_id, None, "read_retry_checked", {
            "request_id": request_id, "page": page, "evidence": manifest,
        })
        if page not in self._normal_pages():
            if page == "rate_limited":
                if states:
                    self.repo.block(self.scopes, reason="rate_limit_after_probe", probe_used=True)
                    self.repo.update_state(task_id, "", "needs_attention", "rate_limit_after_probe")
                else:
                    self.repo.block(
                        self.scopes, reason=page,
                        until=self.now() + timedelta(seconds=self.policy.cooldown_seconds),
                    )
                    self.repo.update_state(task_id, "", "cooldown", "rate_limited")
            else:
                scopes = self.scopes if page == "restricted" else self.scopes[1:]
                self.repo.block(scopes, reason=page)
                self.repo.update_state(task_id, "", "needs_attention", f"read_retry_check:{page}")
            raise PageError(f"read_retry_check:{page}")
        check_cancelled()
        if states:
            self.repo.clear_blocks(self.scopes)
            self.repo.event(task_id, None, "recovery_verified", {"page": page})
            self.repo.update_state(task_id, "", "paused", self.task.stop_reason)
        return self.repo.grant_read_retry(task_id, request_id, now=self.now())

    def execute(self, task_id: str, *, acknowledge: bool = False) -> dict:
        self.task_id = task_id
        self.task = self.repo.task(task_id)
        self.policy = Policy.model_validate(self.task.policy)
        self.scopes = [
            "platform:xiaohongshu",
            f"session:{self.task.session_ref}",
            f"device:{self.task.serial_hash}",
        ]
        profile_hash = hashlib.sha256(self.profile.model_dump_json().encode()).hexdigest()
        self.last_snapshot = self.last_state = None
        self.pending_page_progress = None
        self.run_id = self.repo.start_run(task_id, profile_hash)
        try:
            while True:
                try:
                    self._preflight(acknowledge)
                    self.repo.update_state(task_id, self.run_id, "running")
                    self._pause_check()
                    if self.repo.task(task_id).eligible_count >= self.task.target:
                        raise StopRun("collected_awaiting_review", "target_already_collected")
                    if self.task.mode == "current":
                        if not self.repo.consume(
                            task_id, "detail_visits", self.policy.max_detail_visits
                        ):
                            raise StopRun("partial", "detail_budget_exhausted")
                        allowed = {"detail"} | (self._normal_pages() & {"share"})
                        snap = self._wait(allowed, "current_detail")
                        snap = self._close_share_overlay(snap, expected={"detail"})
                        self._collect_detail(snap)
                        done = self.repo.task(task_id).eligible_count >= self.task.target
                        raise StopRun(
                            "collected_awaiting_review" if done else "partial",
                            "current_detail_saved" if done else "current_detail_incomplete",
                        )
                    self._search()
                    self._traverse()
                except Cooldown:
                    continue
                except RecoverSearch as exc:
                    if self.task.mode == "current":
                        raise StopRun("paused", "reopen_current_detail_after_device_error") from exc
                    self.repo.event(task_id, self.run_id, "workflow_recovery", {"reason": str(exc)})
                    self._sleep(5)
        except StopRun as stop:
            self.repo.finish(task_id, self.run_id, stop.status, stop.reason)
        except (DeviceError, PageError) as exc:
            if isinstance(exc, PageError):
                self.repo.block(self.scopes[1:], reason=type(exc).__name__)
            self.repo.finish(task_id, self.run_id, "paused", f"{type(exc).__name__}: {exc}")
            self._save_failure(type(exc).__name__, fresh=isinstance(exc, PageError))
        except EvidenceError:
            # No further device actions occur after an evidence write failure.
            self.repo.finish(task_id, self.run_id, "paused", "evidence_save_failed")
            raise
        # Storage errors deliberately propagate; the CLI records an emergency event.
        # Previously committed budgets survive; next owner reconciles a stale running run.
        return self.repo.status(task_id)

    def _pause_check(self) -> None:
        if self.stop_requested() or self.repo.task(self.task_id).pause_requested:
            raise StopRun("paused", "operator_pause")

    def _sleep(self, seconds: float) -> None:
        deadline = self.monotonic() + seconds
        while self.monotonic() < deadline:
            self._pause_check()
            self.sleep(min(1.0, max(0, deadline - self.monotonic())))

    def _attempt(self, step: str, operation: Callable[[], T]) -> T:
        while True:
            self._pause_check()
            if not self.repo.begin_step(self.task_id, step, self.policy.max_retries):
                if step in READ_STEPS:
                    raise StopRun("paused", "consecutive_read_failures:10")
                raise DeviceError(f"{step}: persistent retry budget exhausted")
            try:
                result = operation()
            except DeviceError as exc:
                if not self.repo.fail_step(self.task_id, step, self.policy.max_retries):
                    if step in READ_STEPS:
                        self.repo.event(self.task_id, self.run_id, "read_anomaly", {
                            "kind": "consecutive_read_failures", "count": 10, "step": step,
                        })
                        self._save_failure("consecutive_read_failures")
                        raise StopRun("paused", "consecutive_read_failures:10") from exc
                    raise DeviceError(f"{step}: persistent retry budget exhausted") from exc
                if self._recover_connection(step):
                    # Only this read is retried. Its caller must classify the fresh page.
                    continue
                self.repo.event(self.task_id, self.run_id, "device_retry", {"step": step})
                self._sleep(5)
            else:
                self.repo.complete_step(self.task_id, step)
                return result

    def _act(self, step: str, operation: Callable[[], None]) -> None:
        """An uncertain write is never blindly repeated at stale coordinates."""
        self._pause_check()
        if self.last_action is not None:
            self._sleep(max(0, self.policy.action_interval - (self.monotonic() - self.last_action)))
        budget_key = f"action:{step}"
        if not self.repo.begin_step(self.task_id, budget_key, self.policy.max_retries):
            raise DeviceError(f"{step}: persistent retry budget exhausted")
        self.last_action = self.monotonic()
        if self.last_state is not None and step in {
            "search_entry", "input_keyword", "search_submit", "filter_entry",
            "image_text_filter", "filter_confirm", "open_candidate", "back",
            "back_to_results", "back_to_search", "expand_body", "share_close",
        }:
            self.pending_page_progress = (
                page_progress_signature(self.last_state, self.profile), step,
            )
        try:
            operation()
        except DeviceError as exc:
            can_retry = self.repo.fail_step(self.task_id, budget_key, self.policy.max_retries)
            # The click/input may already have reached Android. Recovery must re-observe
            # the page and replay the search workflow, rather than retry the same click.
            if not can_retry:
                raise DeviceError(f"{step}: persistent retry budget exhausted") from exc
            self._recover_connection(step)
            raise RecoverSearch(f"uncertain_action:{step}") from exc
        self.repo.complete_step(self.task_id, budget_key)

    def _recover_connection(self, step: str) -> bool:
        """Repair only proven transport failure, never an automation-service error.

        The failed original step has already been completed in storage before this
        method is entered. Candidate attempts use their own cumulative pre-debit,
        so a process restart cannot erase either budget or overwrite pending work.
        """
        if self.connection is None:
            return False
        self._pause_check()

        def consume_attempt() -> bool:
            self._pause_check()
            return self.repo.consume_retry(
                self.task_id, "connection:attempt", self.policy.max_retries + 1
            )

        try:
            if self.connection.transport_healthy(lock_held=True):
                self._pause_check()
                return False
            self._pause_check()
            self.repo.event(
                self.task_id, self.run_id, "connection_recovery_started", {"step": step}
            )
            serial = self.connection.ensure_connected(
                lock_held=True, consume_attempt=consume_attempt
            )
            self._pause_check()
            if serial != self.device.serial:
                raise StopRun("paused", "connection:device_binding_changed")
        except ConnectionFailure as exc:
            self._pause_check()
            if exc.code == "cancelled":
                raise StopRun("paused", "operator_pause") from None
            # Never persist cloud exception text, which may contain credentials.
            allowed = {
                "offline", "configuration", "credentials", "authorization", "cloud_timeout",
                "connection_timeout", "journal_pending", "connection_recovery_exhausted",
            }
            code = exc.code if exc.code in allowed else "connection_failed"
            self.repo.event(
                self.task_id, self.run_id, "connection_recovery_failed", {"code": code}
            )
            self._save_failure("connection_recovery_failed")
            raise StopRun("paused", f"connection:{code}") from None
        self.repo.event(self.task_id, self.run_id, "connection_recovered", {"step": step})
        return True

    def _capture(self) -> Snapshot:
        started = self.monotonic()
        snap = self._attempt("capture", self.device.capture)
        self.last_snapshot = self.last_state = snap
        self.repo.event(self.task_id, self.run_id, "stage_timing", {
            "stage": "evidence_capture", "seconds": self.monotonic() - started,
            "components": snap.metadata.get("capture_timings", {}),
        })
        return snap

    def _read_state(self) -> UIState:
        started = self.monotonic()
        state = self._attempt("read_state", self.device.read_state)
        self.last_state = state
        self.repo.event(self.task_id, self.run_id, "stage_timing", {
            "stage": "navigation_read", "seconds": self.monotonic() - started,
            "components": state.metadata.get(
                "read_timings", state.metadata.get("capture_timings", {}),
            ),
        })
        return state

    def _save(self, snapshot: Snapshot, label: str) -> dict:
        if not isinstance(snapshot, Snapshot):
            raise EvidenceError("Full screenshot evidence requires Snapshot, not UIState")
        return self.evidence.save(
            snapshot, device_id=self.task.device_id, run_id=self.run_id, label=label,
        )

    def _save_failure(self, label: str, *, fresh: bool = False) -> None:
        if fresh and self.last_state is not None and not isinstance(self.last_state, Snapshot):
            try:
                self.last_snapshot = self.device.capture()
                self.last_state = self.last_snapshot
            except DeviceError:
                pass
        # A failed read may leave only an older full frame. Preserve its timestamp;
        # never attach a fresh UI tree to an unrelated old PNG.
        if self.last_snapshot is not None:
            manifest = self._save(self.last_snapshot, label)
            self.repo.event(self.task_id, self.run_id, "failure_evidence", {
                "evidence": manifest, "fresh": self.last_state is self.last_snapshot,
            })

    def _alert_evidence(self, state: UIState | Snapshot, label: str) -> dict | None:
        if isinstance(state, Snapshot):
            return self._save(state, label)
        try:
            # The blocking policy is durable before diagnostics. One capture only,
            # so an evidence read cannot silently spend ten retries or re-navigate.
            snap = self.device.capture()
        except DeviceError:
            self.repo.event(self.task_id, self.run_id, "alert_evidence_unavailable", {
                "page": label, "state_captured_at": state.captured_at.isoformat(),
            })
            return None
        self.last_snapshot = self.last_state = snap
        return self._save(snap, label)

    def _check_alert(self, page: str, snapshot: UIState | Snapshot) -> None:
        if page not in {"rate_limited", "login", "verification", "restricted"}:
            return
        if page == "rate_limited":
            prior = self.repo.policy_states(self.scopes)
            if any(state.probe_used for state in prior):
                self.repo.block(self.scopes, reason="rate_limit_after_probe", probe_used=True)
                manifest = self._alert_evidence(snapshot, page)
                self.repo.event(
                    self.task_id, self.run_id, "page_alert", {"page": page, "evidence": manifest}
                )
                raise StopRun("needs_attention", "rate_limit_after_probe")
            until = self.now() + timedelta(seconds=self.policy.cooldown_seconds)
            self.repo.block(self.scopes, reason=page, until=until)
            self.repo.update_state(self.task_id, self.run_id, "cooldown", "rate_limited")
            manifest = self._alert_evidence(snapshot, page)
            self.repo.event(
                self.task_id, self.run_id, "page_alert", {"page": page, "evidence": manifest}
            )
            raise Cooldown()
        scopes = self.scopes if page == "restricted" else self.scopes[1:]
        self.repo.block(scopes, reason=page)
        manifest = self._alert_evidence(snapshot, page)
        self.repo.event(
            self.task_id, self.run_id, "page_alert", {"page": page, "evidence": manifest}
        )
        raise StopRun("needs_attention", page)

    def _preflight(self, acknowledge: bool) -> None:
        states = self.repo.policy_states(self.scopes)
        if not states:
            return
        manual = any(state.status == "manual" for state in states)
        if manual and not acknowledge:
            raise StopRun("needs_attention", "manual_resolution_required; use resume --acknowledge")
        until_values = [aware(s.until) for s in states if s.until is not None]
        until = max(until_values) if until_values else None
        if until and until > self.now():
            # Resume waits; creating a fresh task with the same scope cannot clear cooldown.
            self.repo.event(
                self.task_id, self.run_id, "cooldown_wait", {"until": until.isoformat()}
            )
            self.repo.update_state(self.task_id, self.run_id, "cooldown", "waiting_for_cooldown")
            self._sleep((until - self.now()).total_seconds())
        if not manual and any(state.probe_used for state in states):
            self.repo.block(self.scopes, reason="previous_probe_interrupted", probe_used=True)
            raise StopRun("needs_attention", "previous_probe_interrupted")
        self.repo.claim_probe(self.scopes)
        snap = self._capture()
        page = classify(snap, self.profile)
        self._check_alert(page, snap)
        if page not in self._normal_pages():
            self._save_failure("preflight_unknown")
            raise StopRun("needs_attention", "recovery_page_not_confirmed")
        self.repo.clear_blocks(self.scopes)
        self.repo.event(self.task_id, self.run_id, "recovery_verified", {"page": page})

    def _wait(self, expected: set[str], step: str, *, full: bool = False) -> UIState | Snapshot:
        started = self.monotonic()
        observed_loading = False
        while True:
            deadline = self.monotonic() + self.policy.page_timeout
            while True:
                self._pause_check()
                snap = self._capture() if full else self._read_state()
                foreground = snap.metadata.get("package", snap.metadata.get("app_package"))
                if foreground and foreground != self.profile.app_package:
                    self._alert_evidence(snap, "app_left_foreground")
                    if not self.repo.fail_step(
                        self.task_id, "app_restart", self.policy.max_retries,
                    ):
                        raise ProfileError("App left foreground; restart budget exhausted")
                    raise RecoverSearch("App left foreground; replay search from observed state")
                page = classify(snap, self.profile)
                self._check_alert(page, snap)
                if page in expected:
                    if self.pending_page_progress is not None:
                        before, action = self.pending_page_progress
                        self.pending_page_progress = None
                        changed = before != page_progress_signature(snap, self.profile)
                        self._page_progress(observed_loading or changed, action)
                    elif observed_loading:
                        self._page_progress(True, step)
                    self.repo.event(self.task_id, self.run_id, "stage_timing", {
                        "stage": "page_wait", "step": step,
                        "seconds": self.monotonic() - started, "full_evidence": full,
                    })
                    return snap
                if page != "loading":
                    self._alert_evidence(snap, "unknown_page")
                    raise UnknownPage(f"{step}: observed {page}, expected {sorted(expected)}")
                observed_loading = True
                if self.monotonic() >= deadline:
                    break
                self._sleep(1)
            # A readable loading page does not constitute forward progress. Past
            # lifetime waits are kept as diagnostics but no longer block this run.
            if not self.repo.fail_step(self.task_id, f"wait:{step}", self.policy.max_retries):
                self.repo.event(self.task_id, self.run_id, "read_anomaly", {
                    "kind": "consecutive_no_progress", "count": 10, "step": step,
                })
                self._alert_evidence(snap, "page_wait_no_progress")
                raise StopRun("paused", "consecutive_no_progress:10")
            self._sleep(5)

    def _click_action(self, snap: Snapshot | UIState, action: str) -> None:
        bounds = locate(snap, self.profile, action)
        if bounds is None:
            raise UnknownPage(f"Missing or ambiguous action: {action}")
        self._act(action, lambda: self.device.click(bounds))

    def _search(self) -> None:
        started = self.monotonic()
        self._act("launch", lambda: self.device.start_app(self.profile.app_package))
        snap = self._wait(self._normal_pages(), "launch")
        snap = self._close_share_overlay(snap, expected={"detail", "video"})
        for _ in range(4):
            page = classify(snap, self.profile)
            if page in {"home", "search"}:
                break
            self._act("back_to_search", lambda: self.device.press("back"))
            snap = self._wait(self._normal_pages(), "back_to_search")
            snap = self._close_share_overlay(snap, expected={"detail", "video"})
        else:
            raise UnknownPage("Cannot return to a calibrated search entry")
        if classify(snap, self.profile) == "home":
            self._click_action(snap, "search_entry")
            snap = self._wait({"search"}, "search_entry")
        self._click_action(snap, "search_input")
        self._wait({"search"}, "search_input")
        self._act("input_keyword", lambda: self.device.input_text(self.task.keyword))
        snap = self._wait({"search"}, "keyword_input")
        selector = self.profile.actions["search_input"]
        nodes = matching(xml_root(snap), selector)
        expected_text = self.profile.search_input_text_prefix + self.task.keyword
        if len(nodes) != 1 or nodes[0].get("text") != expected_text:
            raise UnknownPage("Chinese keyword input did not read back exactly")
        self._click_action(snap, "search_submit")
        snap = self._wait({"results"}, "search_results")
        if "filter_entry" in self.profile.actions:
            self._click_action(snap, "filter_entry")
            snap = self._wait({"filter"}, "filter_entry")
            selected = matching(xml_root(snap), self.profile.image_text_selected_marker)
            if len(selected) > 1:
                raise UnknownPage("Image-text filter selection marker is ambiguous")
            if not selected:
                self._click_action(snap, "image_text_filter")
                snap = self._wait({"filter"}, "image_text_filter")
                selected = matching(xml_root(snap), self.profile.image_text_selected_marker)
            if len(selected) != 1:
                raise UnknownPage("Image-text filter selection was not uniquely confirmed")
            self.repo.event(self.task_id, self.run_id, "image_text_filter_verified", {})
            self._click_action(snap, "filter_confirm")
            self._wait({"results"}, "filter_confirm")
        elif "image_text_filter" in self.profile.actions:
            self._click_action(snap, "image_text_filter")
            self._wait({"results"}, "image_text_filter")

        self.repo.event(self.task_id, self.run_id, "stage_timing", {
            "stage": "search_navigation", "seconds": self.monotonic() - started,
        })

    def _collect_detail(self, state: Snapshot | UIState) -> bool:
        started = self.monotonic()
        identity = self.identity_reader(self, state)
        identity = identity or {}
        self.repo.event(self.task_id, self.run_id, "stage_timing", {
            "stage": "identity_acquisition", "seconds": self.monotonic() - started,
            "identity_confirmed": bool(identity.get("note_id")),
        })
        manifests = list(identity.get("evidence", []))
        if identity.get("note_id") and self.repo.seen(self.task_id, identity["note_id"]):
            self.repo.event(self.task_id, self.run_id, "known_note_skipped", {
                "note_id": identity["note_id"], "evidence": manifests,
                "before_detailed_capture": True,
                "identity_proof": identity.get("identity_proof"),
            })
            return False
        # Full capture is taken after returning from the optional share panel.
        snap = self._wait({"detail"}, "detail_evidence", full=True)
        if identity.get("note_id") and identity.get("state") is not None:
            from xhs_mobile.identity import _visit_anchor
            if _visit_anchor(identity["state"], self.profile) != _visit_anchor(snap, self.profile):
                self.repo.event(self.task_id, self.run_id, "identity_unconfirmed", {
                    "reason": "detail_changed_after_copy_before_evidence",
                })
                identity = {}
        if identity.get("identity_proof"):
            snap.metadata["identity_proof"] = identity["identity_proof"]
        manifest = self._save(snap, "detail_initial")
        manifests.append(manifest)
        note = parse_note(snap, self.profile, self.ocr)
        if identity.get("note_id"):
            if note.note_id and note.note_id != identity["note_id"]:
                raise UnknownPage("Detail identity changed after copying note link")
            note.note_id = identity["note_id"]
            note.canonical_url = identity.get("canonical_url")
            note.identity_source = identity.get("identity_source", "fresh_phone_share_link")
        if self.repo.seen(self.task_id, note.note_id):
            self.repo.event(self.task_id, self.run_id, "known_note_skipped", {
                "note_id": note.note_id, "evidence": manifests,
            })
            return False
        best_note, best_snapshot, best_evidence = note, snap, manifest

        def attach(parsed: ParsedNote, evidence: dict) -> None:
            reference = evidence.get("manifest_path") or evidence.get("evidence_id")
            for value in parsed.fields.values():
                if value.status == "present":
                    value.evidence_ref = reference
            for source in parsed.topic_sources:
                source["evidence_ref"] = reference

        attach(note, manifest)

        def quality(parsed: ParsedNote) -> tuple[bool, int, int, int]:
            readable = {name for name, value in parsed.fields.items()
                        if value.status == "present" and bool(value.raw)}
            base = len(readable & {"title", "body", "author"})
            if parsed.fields["title"].status == "not_displayed":
                base += 1
            body = parsed.fields.get("body")
            body_length = len(body.raw or "") if body and body.status == "present" else 0
            return parsed.eligible, base, body_length, len(readable)

        def consider(current: Snapshot, label: str) -> ParsedNote:
            nonlocal best_note, best_snapshot, best_evidence
            evidence = self._save(current, label)
            manifests.append(evidence)
            parsed = parse_note(current, self.profile, self.ocr)
            attach(parsed, evidence)
            if parsed.note_id and best_note.note_id and parsed.note_id != best_note.note_id:
                raise UnknownPage("Detail changed during supplemental read")
            for name in ("title", "author"):
                previous, current_value = best_note.fields.get(name), parsed.fields.get(name)
                if (previous and current_value and previous.status == "present"
                        and current_value.status == "present" and previous.raw and current_value.raw
                        and previous.raw != current_value.raw):
                    raise UnknownPage(f"Detail {name} changed during supplemental read")
            # Base fields always come from one frame. Optional fields may be
            # supplemented only when the unchanged note is positively anchored.
            def same_readable(name: str) -> bool:
                prior = best_note.fields.get(name)
                current_value = parsed.fields.get(name)
                return bool(prior and current_value and prior.status == "present"
                            and current_value.status == "present" and current_value.raw
                            and prior.raw == current_value.raw)

            anchored = bool(parsed.note_id and parsed.note_id == best_note.note_id) or (
                same_readable("author") and (same_readable("title") or same_readable("body"))
            )
            if identity.get("note_id") and anchored:
                parsed.note_id, parsed.canonical_url = best_note.note_id, best_note.canonical_url
                parsed.identity_source = best_note.identity_source
            if quality(parsed) > quality(best_note):
                previous = best_note
                best_note, best_snapshot, best_evidence = parsed, current, evidence
                supplement = previous
            else:
                supplement = parsed
            if anchored:
                for name in ("published_at", "tags"):
                    existing, extra = best_note.fields.get(name), supplement.fields.get(name)
                    if (existing and extra and existing.status != "present"
                            and extra.status == "present"):
                        best_note.fields[name] = extra
                        if name == "published_at":
                            best_note.time_kind = supplement.time_kind
                        else:
                            best_note.topics_status = supplement.topics_status
                            best_note.topics = list(supplement.topics)
                            best_note.topic_sources = list(supplement.topic_sources)
            return parsed

        # Expansion reads actual text; it is never triggered by screen geometry
        # or a guessed completeness flag. Each visit expands at most once.
        if locate(snap, self.profile, "expand_body") is not None:
            self._click_action(snap, "expand_body")
            snap = self._wait({"detail"}, "expand_body", full=True)
            note = consider(snap, "detail_expanded")
        for index in range(self.profile.detail_body_swipes):
            missing_base = not best_note.eligible
            missing_time = ("published_at" in self.profile.fields
                            and best_note.fields["published_at"].status != "present")
            missing_topics = ("tags" in self.profile.fields
                              and self.profile.fields["tags"].platform_topic
                              and best_note.fields["tags"].status != "present")
            if not (missing_base or missing_time or missing_topics):
                break
            if not self.repo.consume_retry(
                self.task_id, "body_swipes",
                self.policy.max_detail_visits * self.profile.detail_body_swipes,
            ):
                break
            before = page_progress_signature(snap, self.profile)
            from xhs_mobile.identity import _visit_anchor
            before_anchor = _visit_anchor(snap, self.profile)
            self._act("scroll_detail_body", lambda: self.device.swipe("up"))
            snap = self._wait({"detail"}, "scroll_detail_body", full=missing_base)
            self._page_progress(
                before != page_progress_signature(snap, self.profile), "scroll_detail_body",
            )
            after_anchor = _visit_anchor(snap, self.profile)
            for name in ("title", "author"):
                if (before_anchor.get(name) and after_anchor.get(name)
                        and before_anchor[name] != after_anchor[name]):
                    raise UnknownPage(f"Detail {name} changed during supplemental read")
            if not missing_base:
                # Optional fields use only their calibrated XML roles as a probe.
                # UIState is never parsed as a full record or saved as PNG evidence.
                root = xml_root(snap)
                found = []
                for name, needed in (("published_at", missing_time), ("tags", missing_topics)):
                    if not needed:
                        continue
                    rule = self.profile.fields[name]
                    nodes = matching(root, rule.selector)
                    if (nodes and (rule.many or len(nodes) == 1)
                            and any(node.get(rule.attribute, "").strip() for node in nodes)):
                        found.append(name)
                self.repo.event(self.task_id, self.run_id, "optional_field_probe", {
                    "swipe": index + 1, "ui_fields_found": found, "full_capture": bool(found),
                })
                if not found:
                    continue
                snap = self._wait({"detail"}, "optional_field_evidence", full=True)
            note = consider(snap, f"detail_body_scroll_{index + 1}")
        note, snap = best_note, best_snapshot
        observation_id, saved = self.repo.save_note(
            observation_id=identifier(), task_id=self.task_id, run_id=self.run_id,
            note=note, snapshot=snap, evidence=manifests,
        )
        self.repo.event(self.task_id, self.run_id, "observation_saved", {
            "observation_id": observation_id, "eligible": note.eligible,
            "identity_confirmed": bool(note.note_id), "saved": saved,
            "selected_evidence_id": best_evidence.get("evidence_id"),
        })
        self.repo.event(self.task_id, self.run_id, "stage_timing", {
            "stage": "detail_processing", "seconds": self.monotonic() - started,
        })
        return saved and note.eligible

    def _page_progress(self, advanced: bool, step: str) -> None:
        count = self.repo.page_progress(self.task_id, advanced=advanced)
        if count >= 10:
            self.repo.event(self.task_id, self.run_id, "read_anomaly", {
                "kind": "consecutive_no_progress", "count": count, "step": step,
            })
            raise StopRun("paused", "consecutive_no_progress:10")

    def _traverse(self) -> None:
        # Volatile hints are intentionally reset on every resume.
        attempted: set[tuple[int, int, int, int]] = set()
        previous_view: tuple = ()
        progress_this_view = False
        ready_results: Snapshot | UIState | None = None
        while True:
            self._pause_check()
            task = self.repo.task(self.task_id)
            if task.eligible_count >= task.target:
                raise StopRun("collected_awaiting_review", "target_collected")
            # Returning or scrolling already captured and checked this results
            # page. Consume that observation once, before any further UI action.
            # Keep it local: recovery, pause/resume and a new traversal must read
            # a fresh page instead of retaining previous coordinates.
            snap = ready_results if ready_results is not None else self._wait(
                {"results"}, "traversal"
            )
            ready_results = None
            cards = candidates(snap, self.profile)
            available = [card for card in cards if card.bounds not in attempted]
            if available:
                card = available[0]
                attempted.add(card.bounds)
                if not self.repo.consume(
                    self.task_id,
                    "detail_visits",
                    self.policy.max_detail_visits,
                ):
                    raise StopRun("partial", "detail_budget_exhausted")
                self._act("open_candidate", lambda card=card: self.device.click(card.bounds))
                detail = self._wait({"detail", "video"}, "open_candidate")
                if classify(detail, self.profile) == "detail":
                    progress_this_view = self._collect_detail(detail) or progress_this_view
                else:
                    manifest = self._save(self._capture(), "video_skipped")
                    self.repo.event(
                        self.task_id, self.run_id, "video_skipped", {"evidence": manifest}
                    )
                if "back_to_results" in self.profile.actions:
                    # Re-observe after any expansion; old detail bounds may no longer apply.
                    latest = self._wait({"detail", "video"}, "before_back")
                    self._click_action(latest, "back_to_results")
                else:
                    self._act("back", lambda: self.device.press("back"))
                ready_results = self._wait({"results"}, "return_results")
                continue
            if not self.repo.consume(self.task_id, "list_swipes", self.policy.max_list_swipes):
                raise StopRun("partial", "swipe_budget_exhausted")
            view = page_progress_signature(snap, self.profile, candidates_only=True)
            self._act("scroll_results", lambda: self.device.swipe("up"))
            after = self._wait({"results"}, "scroll_results")
            next_view = page_progress_signature(after, self.profile, candidates_only=True)
            changed = bool(next_view) and next_view != view and next_view != previous_view
            self._page_progress(changed, "scroll_results")
            if changed:
                self.repo.no_progress(
                    self.task_id, made_progress=progress_this_view or bool(next_view),
                )
            previous_view = view
            progress_this_view = False
            # Identical viewport: do not re-open the same coordinates without new evidence.
            if changed:
                attempted.clear()
            ready_results = after
