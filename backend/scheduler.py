"""v0.8.0 — Autonomous Execution Engine: Task Scheduler.

Provides an opt-in, in-process priority queue for PK-Ninja-Agent tasks.

Design goals
------------
* **Backward compatible** — the scheduler is *off* by default
  (``SCHEDULER_ENABLED=false``). When disabled, ``POST /api/tasks`` keeps
  using the original fire-and-forget ``start_task()`` path so all existing
  behaviour and tests are preserved.
* **Thread-safe** — all mutations go through a single ``threading.Lock``.
* **Priority based** — lower priority number == higher importance
  (mirrors Unix ``nice``). Ties broken by enqueue sequence (FIFO).
* **Lifecycle control** — enqueue / pause / resume / cancel / retry /
  reorder, with full status tracking.
* **DB-aware** — queue state is mirrored to the existing ``tasks`` table so
  the history / recovery subsystems can reason about queued jobs.

The scheduler is intentionally *dumb* about *how* a task runs: it only decides
*when* a task should start. The actual execution is delegated to
:func:`backend.agent.start_task` (direct mode) or the background Worker
(:mod:`backend.worker`, Phase 2).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Queue item status ──────────────────────────────────────────────────────

class QueueStatus(str, Enum):
    """Lifecycle status for a queued task."""

    QUEUED = "queued"        # waiting to be picked up by a worker
    RUNNING = "running"      # handed off to execution (worker / start_task)
    PAUSED = "paused"        # held by the operator; will not run until resumed
    CANCELLED = "cancelled"  # operator cancelled before/during execution
    DONE = "done"            # finished (success or failure handled by task status)
    FAILED = "failed"        # exhausted retries and still failing


# ── Queue entry ────────────────────────────────────────────────────────────

@dataclass(order=True)
class _HeapEntry:
    """Internal heap entry. ``order=True`` makes tuples comparable on the
    first fields only — priority, then sequence — never on the payload."""

    priority: int
    sequence: int
    # payload excluded from ordering via ``field(compare=False)``
    task_id: str = field(compare=False)


@dataclass
class QueueItem:
    """Public representation of a queued task."""

    task_id: str
    description: str
    repo_full: str
    priority: int
    status: QueueStatus
    retries: int
    max_retries: int
    enqueued_at: float
    started_at: Optional[float] = None
    error: Optional[str] = None
    # internal monotonic sequence used for stable FIFO ordering in list_items
    _seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "repo_full": self.repo_full,
            "priority": self.priority,
            "status": self.status.value,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "enqueued_at": self.enqueued_at,
            "started_at": self.started_at,
            "error": self.error,
        }


# ── Scheduler ──────────────────────────────────────────────────────────────

class TaskScheduler:
    """In-process priority task scheduler.

    The scheduler maintains:

    * ``_heap`` — a min-heap of ready (``QUEUED``) entries ordered by
      ``(priority, sequence)``.
    * ``_items`` — a flat dict mapping ``task_id -> QueueItem`` for O(1)
      lookup regardless of status.
    """

    def __init__(
        self,
        *,
        default_priority: int = 5,
        default_retries: int = 1,
    ) -> None:
        self._default_priority = default_priority
        self._default_retries = default_retries
        self._lock = threading.Lock()
        self._heap: List[_HeapEntry] = []
        self._counter = itertools.count()  # monotonic sequence for FIFO tie-break
        self._items: Dict[str, QueueItem] = {}

    # ── configuration ──────────────────────────────────────────────────────

    def configure(
        self,
        *,
        default_priority: Optional[int] = None,
        default_retries: Optional[int] = None,
    ) -> None:
        with self._lock:
            if default_priority is not None:
                self._default_priority = default_priority
            if default_retries is not None:
                self._default_retries = default_retries

    # ── enqueue ────────────────────────────────────────────────────────────

    def enqueue(
        self,
        task_id: str,
        description: str,
        repo_full: str,
        *,
        priority: Optional[int] = None,
        max_retries: Optional[int] = None,
        enqueued_at: float,
    ) -> QueueItem:
        """Add a task to the queue. Returns the created :class:`QueueItem`."""
        prio = self._default_priority if priority is None else priority
        retries = self._default_retries if max_retries is None else max_retries
        item = QueueItem(
            task_id=task_id,
            description=description,
            repo_full=repo_full,
            priority=prio,
            status=QueueStatus.QUEUED,
            retries=0,
            max_retries=retries,
            enqueued_at=enqueued_at,
        )
        with self._lock:
            if task_id in self._items:
                raise ValueError(f"task {task_id} already in queue")
            item._seq = next(self._counter)
            self._items[task_id] = item
            heapq.heappush(
                self._heap,
                _HeapEntry(priority=prio, sequence=item._seq, task_id=task_id),
            )
        logger.info("scheduler: enqueued task=%s priority=%s", task_id, prio)
        return item

    # ── pop next ───────────────────────────────────────────────────────────

    def pop_next(self) -> Optional[QueueItem]:
        """Pop the highest-priority *ready* (``QUEUED``) task, if any.

        Skips entries that were paused/cancelled after enqueueing (their
        ``_HeapEntry`` stays in the heap but is discarded lazily here).
        """
        with self._lock:
            while self._heap:
                entry = heapq.heappop(self._heap)
                item = self._items.get(entry.task_id)
                if item is None:
                    continue  # removed
                if item.status == QueueStatus.QUEUED:
                    item.status = QueueStatus.RUNNING
                    return item
                # otherwise skip (paused / cancelled / done / failed)
            return None

    def peek_next(self) -> Optional[QueueItem]:
        """Return the next ready task *without* removing it."""
        with self._lock:
            # rebuild a transient view without mutating the heap
            for entry in sorted(self._heap):
                item = self._items.get(entry.task_id)
                if item and item.status == QueueStatus.QUEUED:
                    return item
            return None

    # ── lifecycle controls ─────────────────────────────────────────────────

    def pause(self, task_id: str) -> Optional[QueueItem]:
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            if item.status in (QueueStatus.QUEUED, QueueStatus.RUNNING):
                item.status = QueueStatus.PAUSED
                logger.info("scheduler: paused task=%s", task_id)
            return item

    def resume(self, task_id: str) -> Optional[QueueItem]:
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            if item.status == QueueStatus.PAUSED:
                item.status = QueueStatus.QUEUED
                # re-insert into heap so it can be picked up again
                heapq.heappush(
                    self._heap,
                    _HeapEntry(
                        priority=item.priority,
                        sequence=next(self._counter),
                        task_id=task_id,
                    ),
                )
                logger.info("scheduler: resumed task=%s", task_id)
            return item

    def cancel(self, task_id: str) -> Optional[QueueItem]:
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            if item.status not in (QueueStatus.DONE, QueueStatus.FAILED, QueueStatus.CANCELLED):
                item.status = QueueStatus.CANCELLED
                logger.info("scheduler: cancelled task=%s", task_id)
            return item

    def retry(self, task_id: str) -> Optional[QueueItem]:
        """Manually re-queue a failed/cancelled/done task for another attempt."""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            if item.retries >= item.max_retries:
                # still allow manual retry but bump the cap so it can run
                item.max_retries = item.retries + 1
            item.retries += 1
            item.status = QueueStatus.QUEUED
            item.error = None
            item.started_at = None
            heapq.heappush(
                self._heap,
                _HeapEntry(
                    priority=item.priority,
                    sequence=next(self._counter),
                    task_id=task_id,
                ),
            )
            logger.info("scheduler: retried task=%s attempt=%s", task_id, item.retries)
            return item

    def mark_done(self, task_id: str) -> Optional[QueueItem]:
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            item.status = QueueStatus.DONE
            return item

    def mark_failed(self, task_id: str, error: str) -> Optional[QueueItem]:
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            item.status = QueueStatus.FAILED
            item.error = error
            # auto-retry if budget remains
            if item.retries < item.max_retries:
                item.retries += 1
                item.status = QueueStatus.QUEUED
                item.error = None
                heapq.heappush(
                    self._heap,
                    _HeapEntry(
                        priority=item.priority,
                        sequence=next(self._counter),
                        task_id=task_id,
                    ),
                )
                logger.info("scheduler: auto-retry task=%s attempt=%s", task_id, item.retries)
            return item

    def reorder(self, task_id: str, priority: int) -> Optional[QueueItem]:
        """Change the priority of a queued task. Re-inserts into the heap."""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            item.priority = priority
            if item.status == QueueStatus.QUEUED:
                heapq.heappush(
                    self._heap,
                    _HeapEntry(
                        priority=priority,
                        sequence=next(self._counter),
                        task_id=task_id,
                    ),
                )
            logger.info("scheduler: reordered task=%s priority=%s", task_id, priority)
            return item

    def remove(self, task_id: str) -> bool:
        """Fully drop a task from the scheduler. Returns True if it existed."""
        with self._lock:
            return self._items.pop(task_id, None) is not None

    # ── introspection ──────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[QueueItem]:
        with self._lock:
            return self._items.get(task_id)

    def list_items(
        self,
        *,
        status: Optional[QueueStatus] = None,
        repo_full: Optional[str] = None,
    ) -> List[QueueItem]:
        with self._lock:
            items = list(self._items.values())
        if status is not None:
            items = [i for i in items if i.status == status]
        if repo_full is not None:
            items = [i for i in items if i.repo_full == repo_full]
        # ready queue first (by priority), then the rest; FIFO tie-break by seq
        items.sort(key=lambda i: (i.status != QueueStatus.QUEUED, i.priority, i._seq))
        return items

    def queue_length(self) -> int:
        with self._lock:
            return sum(1 for i in self._items.values() if i.status == QueueStatus.QUEUED)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._items.values() if i.status == QueueStatus.RUNNING)

    def clear(self) -> None:
        """Reset the scheduler to an empty state (mainly for tests)."""
        with self._lock:
            self._heap.clear()
            self._items.clear()
            self._counter = itertools.count()


# ── module-level singleton (lazily wired by main.py) ───────────────────────

_SCHEDULER: Optional[TaskScheduler] = None
_SCHEDULER_LOCK = threading.Lock()


def get_scheduler() -> Optional[TaskScheduler]:
    """Return the active scheduler singleton, or ``None`` if disabled."""
    return _SCHEDULER


def init_scheduler(
    *,
    default_priority: int = 5,
    default_retries: int = 1,
) -> TaskScheduler:
    """Create and store the scheduler singleton. Idempotent."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = TaskScheduler(
                default_priority=default_priority,
                default_retries=default_retries,
            )
        else:
            _SCHEDULER.configure(
                default_priority=default_priority,
                default_retries=default_retries,
            )
        return _SCHEDULER


def reset_scheduler() -> None:
    """Drop the singleton (mainly for tests)."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None:
            _SCHEDULER.clear()
        _SCHEDULER = None


__all__ = [
    "QueueStatus",
    "QueueItem",
    "TaskScheduler",
    "get_scheduler",
    "init_scheduler",
    "reset_scheduler",
]
