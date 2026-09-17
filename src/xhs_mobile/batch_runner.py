"""Ordered single-host batches reuse the same per-task device runner and budgets.

The caller owns the physical device lock for the entire batch. This is not a
distributed lease or permission to run the same phone from multiple hosts.
"""

from collections.abc import Callable

from xhs_mobile.batches import BatchRepository
from xhs_mobile.repository import Repository

ATTEMPT_END_REASONS = {
    "no_new_candidates", "detail_budget_exhausted", "swipe_budget_exhausted",
}


def attempt_finished(task) -> bool:
    return task.status == "collected_awaiting_review" or (
        task.status == "partial" and task.stop_reason in ATTEMPT_END_REASONS
    )


class BatchRunner:
    def __init__(
        self, *, repository: Repository, batches: BatchRepository,
        execute_task: Callable, stop_requested: Callable = lambda: False,
        task_finished: Callable = lambda _task_id: None,
    ):
        self.repo, self.batches = repository, batches
        self.execute_task, self.external_stop = execute_task, stop_requested
        self.task_finished = task_finished

    def execute(self, batch_id: str, *, acknowledge: bool = False) -> dict:
        task_ids = self.batches.task_ids(batch_id)
        if not task_ids:
            raise ValueError("批次中没有任务")
        self.batches.update(batch_id, "running")

        def stopped():
            return self.external_stop() or self.batches.batch(batch_id).pause_requested

        try:
            for task_id in task_ids:
                if stopped():
                    self.batches.update(batch_id, "paused", "operator_pause")
                    break
                task = self.repo.task(task_id)
                if attempt_finished(task):
                    continue
                self.execute_task(task_id, stopped, acknowledge)
                # An explicit acknowledgement applies only to the first resumed task.
                acknowledge = False
                self.task_finished(task_id)
                current = self.repo.task(task_id)
                if stopped():
                    self.batches.update(batch_id, "paused", "operator_pause")
                    break
                if not attempt_finished(current):
                    self.batches.update(batch_id, "paused", current.stop_reason or current.status)
                    break
            else:
                partial = any(self.repo.task(task_id).status == "partial" for task_id in task_ids)
                self.batches.update(
                    batch_id, "partial" if partial else "collected_awaiting_review",
                    "one_or_more_targets_not_reached" if partial else "all_targets_collected",
                )
        except Exception as exc:
            # The child task retains its committed checkpoint even if this update also fails.
            try:
                self.batches.update(batch_id, "paused", f"execution_error:{type(exc).__name__}")
            except Exception:
                pass
            raise
        return self.batches.status(batch_id)
