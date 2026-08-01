"""v0.8.0 — Autonomous Execution Engine: Background Worker.

Pulls the next-priority ready task from the :class:`TaskScheduler` and executes
it on a daemon thread, independently of the HTTP request that enqueued it.

Design goals
------------
* **Independent of the caller** — once enqueued, a task runs to completion even
  if the originating HTTP request has long returned.
* **Concurrency-limited** — at most ``WORKER_MAX_CONCURRENCY`` tasks run at
  once; the rest stay queued until a worker is free.
* **Restart-tolerant** — on startup the worker loop re-discovers ``QUEUED``
  tasks in the scheduler and resumes draining them (the scheduler is the source
  of truth, not a transient queue).
* **Graceful stop** — ``stop()`` signals the loop to exit after the current
  poll cycle; running tasks are NOT killed (they finish naturally or are
  cancelled via the scheduler).
* **Failure aware** — on task failure, marks the scheduler item so the
  auto-retry budget kicks in.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Type alias for the execution callback: (task_id, description, repo_full) -> None
# In production this is backend.agent.start_task.
StartTaskFn = Callable[[str, str, str], None]


class BackgroundWorker:
    """Drains the scheduler queue on a daemon thread."""

    def __init__(
        self,
        scheduler,
        start_fn: StartTaskFn,
        *,
        max_concurrency: int = 2,
        poll_interval: float = 1.0,
    ) -> None:
        self._scheduler = scheduler
        self._start_fn = start_fn
        self._max_concurrency = max(1, max_concurrency)
        self._poll_interval = max(0.05, poll_interval)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active: int = 0
        self._active_lock = threading.Lock()
        self._completed = 0
        self._failed = 0

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="pk-ninja-worker", daemon=True,
        )
        self._thread.start()
        logger.info("worker: started (concurrency=%s)", self._max_concurrency)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("worker: stopped (completed=%s failed=%s)",
                    self._completed, self._failed)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return self._active

    @property
    def completed_count(self) -> int:
        return self._completed

    @property
    def failed_count(self) -> int:
        return self._failed

    # ── main loop ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._try_dispatch()
            self._stop_event.wait(self._poll_interval)

    def _try_dispatch(self) -> None:
        with self._active_lock:
            if self._active >= self._max_concurrency:
                return
        item = self._scheduler.pop_next()
        if item is None:
            return
        with self._active_lock:
            self._active += 1
        t = threading.Thread(
            target=self._run_one,
            args=(item,),
            name=f"pk-ninja-task-{item.task_id}",
            daemon=True,
        )
        t.start()

    def _run_one(self, item) -> None:
        logger.info("worker: dispatching task=%s repo=%s", item.task_id, item.repo_full)
        try:
            self._start_fn(item.task_id, item.description, item.repo_full)
            self._scheduler.mark_done(item.task_id)
            self._completed += 1
            logger.info("worker: task=%s done", item.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker: task=%s failed", item.task_id)
            self._scheduler.mark_failed(item.task_id, str(exc))
            self._failed += 1
        finally:
            with self._active_lock:
                self._active -= 1


# ── module-level singleton ─────────────────────────────────────────────────

_WORKER: Optional[BackgroundWorker] = None
_WORKER_LOCK = threading.Lock()


def get_worker() -> Optional[BackgroundWorker]:
    return _WORKER


def init_worker(
    scheduler,
    start_fn: StartTaskFn,
    *,
    max_concurrency: int = 2,
    poll_interval: float = 1.0,
    autostart: bool = True,
) -> BackgroundWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.stop(timeout=1.0)
        _WORKER = BackgroundWorker(
            scheduler,
            start_fn,
            max_concurrency=max_concurrency,
            poll_interval=poll_interval,
        )
        if autostart:
            _WORKER.start()
        return _WORKER


def stop_worker(timeout: float = 5.0) -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.stop(timeout=timeout)
        _WORKER = None


def reset_worker() -> None:
    """Drop the singleton without joining (for tests)."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.stop(timeout=0.5)
        _WORKER = None


__all__ = [
    "BackgroundWorker",
    "StartTaskFn",
    "get_worker",
    "init_worker",
    "stop_worker",
    "reset_worker",
]
